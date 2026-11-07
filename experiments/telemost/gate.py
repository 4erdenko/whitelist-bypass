import argparse
from collections import defaultdict, deque, OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import random
import re
import select
import socket
import threading
import time


class Gate:
    def __init__(self, root, account_rpm=120, ip_rpm=120, fallback=30):
        self.root = root
        self.rates = {'account': account_rpm, 'ip': ip_rpm}
        self.fallback = fallback
        self.condition = threading.Condition()
        self.next_slot = defaultdict(float)
        self.cooldown = defaultdict(float)
        self.last_429 = defaultdict(float)
        self.strikes = defaultdict(int)
        self.inflight = {}
        self.expired = OrderedDict()
        self.completed = OrderedDict()
        self.queue = []
        self.recent = deque()
        self.counts = defaultdict(int)

    def log(self, kind, **fields):
        row = {'utc': datetime.now(timezone.utc).isoformat(), 'kind': kind, **fields}
        with (self.root / 'api-events.jsonl').open('a') as stream:
            stream.write(json.dumps(row) + '\n')

    def keys(self, event):
        return ('account:' + event['account'], 'ip:' + event['egress'])

    def acquire(self, event, timeout=120, cancelled=lambda: False):
        started = time.monotonic()
        keys = self.keys(event)
        with self.condition:
            if any(row['id'] == event['id'] for row in self.queue) or event['id'] in self.inflight:
                raise ValueError('Duplicate request identifier')
            self.queue.append(event)
            try:
                while True:
                    now = time.monotonic()
                    if cancelled():
                        self.log('queue_cancelled', **event)
                        raise ConnectionAbortedError('Requester disconnected')
                    for identifier, lease in list(self.inflight.items()):
                        if now >= lease['expires']:
                            self.log('lease_expired', **lease['event'])
                            self.expired[identifier] = lease
                            if len(self.expired) > 4096:
                                self.expired.popitem(last=False)
                            del self.inflight[identifier]
                    earlier = self.queue[:self.queue.index(event)]
                    blocked = any(set(keys).intersection(self.keys(row)) for row in earlier)
                    busy = any(set(keys).intersection(row['keys']) for row in self.inflight.values())
                    due = max(*(self.next_slot[key] for key in keys), *(self.cooldown[key] for key in keys))
                    if not blocked and not busy and now >= due:
                        for key in keys:
                            rate = self.rates[key.split(':')[0]]
                            self.next_slot[key] = now + 60 / rate * random.uniform(1, 1.1)
                        self.inflight[event['id']] = {'keys': keys, 'expires': now + 40, 'event': event}
                        self.counts['granted'] += 1
                        self.log('granted', **event, queue_seconds=now-started)
                        return
                    if now - started >= timeout:
                        self.counts['queue_timeout'] += 1
                        self.log('queue_timeout', **event, queue_seconds=now-started)
                        raise TimeoutError('API queue deadline')
                    self.condition.wait(min(0.25, timeout-(now-started)))
            finally:
                self.queue.remove(event)
                self.condition.notify_all()

    def release(self, event):
        with self.condition:
            if event['id'] in self.completed:
                if self.keys(event) != self.completed[event['id']]:
                    raise ValueError('Request scope changed')
                return
            lease = self.inflight.pop(event['id'], None)
            late = lease is None
            if late:
                lease = self.expired.pop(event['id'], None)
            if lease is None:
                raise ValueError('Unknown request lease')
            if self.keys(event) != lease['keys']:
                raise ValueError('Request scope changed')
            now = time.monotonic()
            delay = 0
            if event['status'] == 429:
                for key in lease['keys']:
                    if now - self.last_429[key] > 600:
                        self.strikes[key] = 0
                    self.strikes[key] += 1
                    self.last_429[key] = now
                    backoff = min(300, self.fallback * 2 ** min(4, self.strikes[key]-1))
                    delay = max(delay, backoff, event['retry_after_seconds'])
                delay *= random.uniform(1, 1.1)
                for key in lease['keys']:
                    self.cooldown[key] = max(self.cooldown[key], now + delay)
            self.counts[str(event['status'])] += 1
            self.recent.append((now, dict(event)))
            while self.recent and self.recent[0][0] < now-600:
                self.recent.popleft()
            self.log('response', **event, cooldown_seconds=delay, late_release=late)
            self.completed[event['id']] = lease['keys']
            if len(self.completed) > 4096:
                self.completed.popitem(last=False)
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            now = time.monotonic()
            minute = defaultdict(int)
            limits = []
            for when, row in self.recent:
                if when >= now-60:
                    minute[row['egress'] + ':' + row['role']] += 1
                if when >= now-600 and row['status'] == 429:
                    limits.append({k: row[k] for k in ['account', 'egress', 'role', 'room', 'endpoint']})
            return {'counts': dict(self.counts), 'requests_last_minute': dict(minute),
                    'recent_429': limits, 'queued': len(self.queue), 'inflight': len(self.inflight),
                    'cooldowns': {k: v-now for k, v in self.cooldown.items() if v > now}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def disconnected(self):
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def do_GET(self):
        if self.path == '/snapshot':
            self.reply(200, self.server.gate.snapshot())
        else:
            self.reply(404, {})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096:
                raise ValueError('Invalid body size')
            event = json.loads(self.rfile.read(length))
            for key in ['id', 'account', 'egress', 'role', 'room', 'endpoint', 'method']:
                if not isinstance(event.get(key), str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', event[key]):
                    raise ValueError('Invalid label')
            for key in ['retry_after_seconds', 'elapsed_seconds']:
                value = event.get(key, 0)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError('Invalid duration')
            if self.path == '/acquire':
                self.server.gate.acquire(event, cancelled=self.disconnected)
            elif self.path == '/release':
                self.server.gate.release(event)
            else:
                self.reply(404, {})
                return
            self.reply(200, {'ok': True})
        except TimeoutError:
            self.reply(503, {'error': 'queue_deadline'})
        except ConnectionAbortedError:
            pass
        except (ValueError, KeyError, TypeError):
            self.reply(400, {'error': 'invalid_request'})


class GateServer(ThreadingHTTPServer):
    request_queue_size = 128


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18990)
    parser.add_argument('--account-rpm', type=float, default=120)
    parser.add_argument('--ip-rpm', type=float, default=120)
    args = parser.parse_args()
    if not 1 <= args.account_rpm <= 180 or not 1 <= args.ip_rpm <= 180:
        raise SystemExit('Rates must be between 1 and 180 requests per minute')
    os.umask(0o077)
    args.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    server = GateServer(('127.0.0.1', args.port), Handler)
    server.gate = Gate(args.root, args.account_rpm, args.ip_rpm)
    server.serve_forever()
