import argparse
import datetime
import hashlib
import http.client
import json
import socket
import struct
import time

BLOCK = hashlib.shake_256(b"FRDM Telemost synthetic test v1").digest(1024 * 1024)


def exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("SOCKS connection closed")
        data += chunk
    return data


class SocksHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.create_connection(("127.0.0.1", ARGS.socks), timeout=ARGS.timeout)
        self.sock.sendall(b"\x05\x01\x00")
        if exact(self.sock, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS negotiation failed")
        self.sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(self.host) + struct.pack("!H", self.port))
        reply = exact(self.sock, 4)
        if reply[1] != 0:
            raise ConnectionError(f"SOCKS destination rejected: {reply[1]}")
        if reply[3] == 1:
            exact(self.sock, 6)
        elif reply[3] == 4:
            exact(self.sock, 18)
        elif reply[3] == 3:
            exact(self.sock, exact(self.sock, 1)[0] + 2)
        else:
            raise ConnectionError("SOCKS invalid reply")


def run(kind, mib):
    started = time.monotonic()
    result = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "kind": kind, "mib": mib, "route": "socks" if ARGS.socks else "ssh-control"}
    conn = SocksHTTP("127.0.0.1", ARGS.target_port, timeout=ARGS.timeout) if ARGS.socks else http.client.HTTPConnection("127.0.0.1", ARGS.direct, timeout=ARGS.timeout)
    stage = 'connect'
    sent = 0
    count = 0
    deadline = started + ARGS.timeout
    def remaining():
        value = deadline-time.monotonic()
        if value <= 0:
            raise TimeoutError('Total probe deadline')
        if conn.sock is not None:
            conn.sock.settimeout(value)
    try:
        digest = hashlib.sha256()
        if kind == "upload":
            conn.putrequest("POST", "/upload")
            conn.putheader("Content-Length", str(mib * len(BLOCK)))
            conn.putheader("X-Test-ID", result["utc"])
            conn.endheaders()
            stage = 'upload_send'
            for _ in range(mib):
                for offset in range(0,len(BLOCK),32768):
                    remaining()
                    chunk=BLOCK[offset:offset+32768]
                    conn.send(chunk)
                    sent += len(chunk)
                    digest.update(chunk)
            stage = 'upload_response'
            remaining()
            response = conn.getresponse()
            received = json.loads(response.read())
            count = received["bytes"]
            valid = received["sha256"] == digest.hexdigest() and count == mib * len(BLOCK)
        else:
            path = f"/download?mib={mib}" if kind == "download" else "/health"
            if kind == "stream":
                path = f"/stream?seconds={ARGS.duration}"
            conn.request("GET", path)
            stage = 'download_headers'
            remaining()
            response = conn.getresponse()
            stage = 'download_body'
            count = 0
            health = b""
            last_progress = started
            while True:
                remaining()
                chunk=response.read(65536)
                if not chunk:
                    break
                count += len(chunk)
                digest.update(chunk)
                if kind == "health":
                    health += chunk
                if kind == "stream" and time.monotonic() - last_progress >= 60:
                    print(json.dumps({"kind": "stream_progress", "seconds": round(time.monotonic() - started, 1), "bytes": count}), flush=True)
                    last_progress = time.monotonic()
            expected = hashlib.sha256()
            for _ in range(mib):
                expected.update(BLOCK)
            if kind == "stream":
                expected = hashlib.sha256()
                for _ in range(ARGS.duration * 2):
                    expected.update(BLOCK[:16384])
                valid = digest.digest() == expected.digest() and count == ARGS.duration * 32768
            else:
                valid = (digest.digest() == expected.digest() and count == mib * len(BLOCK)) if kind == "download" else json.loads(health)["service"] == "frdm-telemost-synthetic-sink"
        duration = time.monotonic() - started
        result.update(ok=response.status == 200 and valid, status=response.status, bytes=count, seconds=round(duration, 4), mbps=round(count * 8 / duration / 1e6, 3), sha256=digest.hexdigest())
    except Exception as exc:
        result.update(ok=False, seconds=round(time.monotonic() - started, 4), error=f"{type(exc).__name__}: {exc}", stage=stage, sent_bytes=sent, received_bytes=count)
    finally:
        result['over_30_seconds'] = time.monotonic()-started > 30
        conn.close()
    print(json.dumps(result), flush=True)
    return result["ok"]


parser = argparse.ArgumentParser()
parser.add_argument("kind", choices=["health", "download", "upload", "soak", "stream"])
parser.add_argument("--target-port", type=int, default=18093)
parser.add_argument("--socks", type=int, default=18080)
parser.add_argument("--direct", type=int, default=18092)
parser.add_argument("--mib", type=int, default=16)
parser.add_argument("--timeout", type=float, default=90)
parser.add_argument("--duration", type=int, default=3600)
parser.add_argument("--interval", type=int, default=30)
ARGS = parser.parse_args()
if ARGS.kind == "soak":
    deadline = time.monotonic() + ARGS.duration
    iteration = 0
    while time.monotonic() < deadline:
        started = time.monotonic()
        run("health", 0)
        run("upload" if iteration % 2 else "download", ARGS.mib)
        iteration += 1
        time.sleep(max(0, min(deadline - time.monotonic(), ARGS.interval - (time.monotonic() - started))))
else:
    raise SystemExit(0 if run(ARGS.kind, ARGS.mib) else 1)
