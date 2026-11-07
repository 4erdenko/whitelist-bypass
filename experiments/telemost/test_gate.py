from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
import threading

from gate import Gate


def event(identifier, account='a', ip='one'):
    return {'id': identifier, 'account': account, 'egress': ip, 'role': 'creator',
            'room': 'one', 'endpoint': 'connection', 'method': 'GET', 'authenticated': True,
            'status': 200, 'retry_after_seconds': 0, 'retry_after_present': False, 'elapsed_seconds': 0}


class GateTest(unittest.TestCase):
    def test_disconnected_waiter_does_not_keep_the_queue(self):
        with tempfile.TemporaryDirectory() as root:
            gate = Gate(Path(root), 60000, 60000)
            active = event('active')
            gate.acquire(active)
            cancelled = threading.Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                waiting = pool.submit(gate.acquire, event('abandoned'), 5, cancelled.is_set)
                time.sleep(.03)
                cancelled.set()
                with self.assertRaises(ConnectionAbortedError):
                    waiting.result(timeout=1)
            gate.release(active)
            following = event('following')
            gate.acquire(following, timeout=.1)
            gate.release(following)
            self.assertEqual(gate.snapshot()['queued'], 0)

    def test_late_release_is_counted_once_and_keeps_429_backoff(self):
        with tempfile.TemporaryDirectory() as root:
            gate = Gate(Path(root), 60000, 60000)
            row = event('expired')
            gate.acquire(row)
            gate.inflight[row['id']]['expires'] = time.monotonic()-1
            other = event('other', account='b', ip='two')
            gate.acquire(other)
            gate.release(other)
            row.update(status=429, retry_after_seconds=60)
            gate.release(row)
            gate.release(row)
            self.assertEqual(gate.snapshot()['counts']['429'], 1)
            self.assertGreater(gate.snapshot()['cooldowns']['account:a'], 59)

    def test_shared_spacing_across_threads(self):
        with tempfile.TemporaryDirectory() as root:
            gate = Gate(Path(root), 600, 600)
            starts = []
            def run(i):
                row = event(str(i))
                gate.acquire(row)
                starts.append(time.monotonic())
                gate.release(row)
            with ThreadPoolExecutor(max_workers=5) as pool:
                list(pool.map(run, range(5)))
            self.assertTrue(all(b-a >= .099 for a,b in zip(sorted(starts), sorted(starts)[1:])))

    def test_429_blocks_account_across_ips_and_ip_across_accounts(self):
        with tempfile.TemporaryDirectory() as root:
            gate = Gate(Path(root), 60000, 60000, fallback=.15)
            row = event('initial')
            gate.acquire(row)
            row.update(status=429, retry_after_seconds=.3)
            gate.release(row)
            for blocked in [event('same-account', ip='two'), event('same-ip', account='b')]:
                with self.assertRaises(TimeoutError):
                    gate.acquire(blocked, timeout=.02)
            independent = event('independent', account='b', ip='two')
            gate.acquire(independent, timeout=.02)
            gate.release(independent)
            self.assertEqual(gate.snapshot()['counts']['429'], 1)
            self.assertTrue(all(value > .2 for value in gate.snapshot()['cooldowns'].values()))

    def test_strikes_survive_unrelated_success(self):
        with tempfile.TemporaryDirectory() as root:
            gate = Gate(Path(root), 60000, 60000, fallback=.01)
            for i in range(2):
                row = event(str(i))
                gate.acquire(row)
                row.update(status=429)
                gate.release(row)
            self.assertEqual(gate.strikes['account:a'], 2)
            self.assertEqual(gate.snapshot()['counts']['429'], 2)


if __name__ == '__main__':
    unittest.main()
