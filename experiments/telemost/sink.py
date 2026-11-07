import hashlib
import json
import time
import datetime
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

BLOCK = hashlib.shake_256(b"FRDM Telemost synthetic test v1").digest(1024 * 1024)
START = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, body, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == "/health":
            self.reply(json.dumps({"service": "frdm-telemost-synthetic-sink", "uptime": round(time.monotonic() - START, 3)}).encode())
            return
        if path.path == "/stream":
            duration = max(1, min(7200, int(parse_qs(path.query).get("seconds", [60])[0])))
            chunk = BLOCK[:16384]
            self.send_response(200)
            self.send_header("Content-Length", str(duration * 2 * len(chunk)))
            self.end_headers()
            try:
                for _ in range(duration * 2):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path.path != "/download":
            self.send_error(404)
            return
        count = max(1, min(1024, int(parse_qs(path.query).get("mib", [1])[0])))
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(count * len(BLOCK)))
        self.end_headers()
        try:
            for _ in range(count):
                self.wfile.write(BLOCK)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.path != "/upload":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        if not 0 < length <= 1024 * 1024 * 1024:
            self.send_error(400)
            return
        self.connection.settimeout(120)
        remaining = length
        started = time.monotonic()
        request_id = self.headers.get("X-Test-ID", "unknown")
        print(json.dumps({"kind":"upload_start","request":request_id,"bytes":length}),flush=True)
        digest = hashlib.sha256()
        while remaining:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
            print(json.dumps({"kind":"upload_progress","request":request_id,"bytes":length-remaining,"seconds":round(time.monotonic()-started,3)}),flush=True)
        self.reply(json.dumps({"bytes": length - remaining, "sha256": digest.hexdigest()}).encode())


class SinkServer(ThreadingHTTPServer):
    request_queue_size = 128


parser = argparse.ArgumentParser()
parser.add_argument("--port",type=int,default=18093)
args = parser.parse_args()
SinkServer(("127.0.0.1", args.port), Handler).serve_forever()
