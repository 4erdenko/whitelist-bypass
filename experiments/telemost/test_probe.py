import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest


class SyntheticProbeTest(unittest.TestCase):
    def test_health_and_payload_hashes_against_a_real_loopback_sink(self):
        scripts = Path(__file__).parent
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        sink = subprocess.Popen([sys.executable, str(scripts/'sink.py'), '--port', str(port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.02)
            for kind in ['health', 'download', 'upload']:
                result = subprocess.run([sys.executable, str(scripts/'bench.py'), kind,
                                         '--socks', '0', '--direct', str(port), '--mib', '1', '--timeout', '3'],
                                        capture_output=True, text=True, timeout=5, check=True)
                row = json.loads(result.stdout)
                self.assertTrue(row['ok'])
                if kind != 'health':
                    self.assertEqual(row['bytes'], 1048576)
                    self.assertEqual(row['sha256'], '67fc600a3991488f9d6970b4a1796201d5910fd97eb05c8f02ce590fc686dd56')
        finally:
            sink.terminate()
            sink.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
