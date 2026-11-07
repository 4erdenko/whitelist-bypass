import subprocess
import unittest

from ssh_control import invoke


class SSHControlTest(unittest.TestCase):
    def test_transient_transport_failure_is_bounded_and_reported(self):
        calls = []
        diagnostics = []
        recovered = []
        def run(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return subprocess.CompletedProcess(args, 255, '', 'Connection reset')
            return subprocess.CompletedProcess(args, 0, '{"active":true}', '')
        result = invoke(['ssh'], {'action':'status'}, lambda *x:diagnostics.append(x), recovered.append, run=run, sleep=lambda _:None)
        self.assertTrue(result['active'])
        self.assertEqual(recovered, [1])
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(len(calls), 2)

    def test_persistent_failure_stops_after_three_attempts(self):
        calls = []
        def run(*args, **kwargs):
            calls.append(1)
            raise subprocess.TimeoutExpired(args, 45)
        with self.assertRaisesRegex(RuntimeError, 'after 3 attempts'):
            invoke(['ssh'], {'action':'status'}, lambda *x:None, lambda x:None, run=run, sleep=lambda _:None)
        self.assertEqual(len(calls), 3)

    def test_remote_program_error_is_not_replayed(self):
        calls = []
        def run(*args, **kwargs):
            calls.append(1)
            return subprocess.CompletedProcess(args, 1, '', 'Failed guard')
        with self.assertRaises(RuntimeError):
            invoke(['ssh'], {'action':'start'}, lambda *x:None, lambda x:None, run=run, sleep=lambda _:None)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
