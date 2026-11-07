import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import remote


class RemoteLifecycleTest(unittest.TestCase):
    def test_prepare_reconciles_a_lost_response_and_cleanup_owns_only_its_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = root/'session.private'
            session.mkdir()
            cookie = session/'account-a.cookies.private.json'
            cookie.write_text('[]')
            active = set()
            commands = []

            def execute(args):
                commands.append(args)
                if args[:2] == ['systemctl', 'is-active']:
                    return subprocess.CompletedProcess(args, 0 if args[2] in active else 3, '', '')
                if args[0] == 'systemd-run':
                    unit = next(value.split('=', 1)[1] for value in args if value.startswith('--unit='))
                    active.add(unit+('.timer' if '--on-active=5h' in args else '.service'))
                if args[:2] == ['systemctl', 'stop']:
                    active.difference_update(args[2:])
                return subprocess.CompletedProcess(args, 0, '', '')

            with patch.object(remote, 'ROOT', root), patch.object(remote, 'SESSION', session), \
                 patch.object(remote, 'run', side_effect=execute), \
                 patch.object(remote, 'states', return_value=[{'ActiveState': 'inactive'}]):
                request = {'action': 'prepare', 'run_id': 'a'*32}
                self.assertTrue(remote.main(request)['ok'])
                self.assertTrue(remote.main(request)['ok'])
                launches = [command for command in commands if command[0] == 'systemd-run']
                self.assertEqual(len(launches), 2)
                self.assertEqual(launches[-1][-1], 'a'*32)
                before = len(commands)
                rejected = remote.main({'action': 'cleanup', 'run_id': 'b'*32})
                self.assertFalse(rejected['ok'])
                self.assertEqual(len(commands), before)
                self.assertTrue(cookie.exists())
                result = remote.main({'action': 'cleanup', 'run_id': 'a'*32})
                self.assertTrue(result['stopped'])
                self.assertTrue(result['session_absent'])
                self.assertFalse((root/'owner.private.json').exists())

    def test_unowned_active_support_service_blocks_a_new_run(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(remote, 'ROOT', Path(folder)), \
                 patch.object(remote, 'states', return_value=[]), \
                 patch.object(remote, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
                result = remote.main({'action': 'prepare', 'run_id': 'a'*32})
                self.assertFalse(result['ok'])
                self.assertTrue(all(call.args[0][0] == 'systemctl' for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
