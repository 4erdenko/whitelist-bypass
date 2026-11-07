import json
from pathlib import Path
import tempfile
import unittest

from lab_config import load_config, validate_plan


class ConfigTest(unittest.TestCase):
    def test_private_configuration_preserves_explicit_host_and_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = root/'test-key'
            key.touch()
            path = root/'config.json'
            config = {'ssh_target': 'root@test.example', 'ssh_key': str(key),
                      'ssh_port': 2222, 'remote_dir': '/opt/telemost-lab'}
            path.write_text(json.dumps(config))
            self.assertEqual(load_config(path), {**config, 'ssh_key': str(key.resolve())})
            for field, value in [('ssh_target', 'root@lab.example.invalid'),
                                 ('ssh_target', 'root@host;command'),
                                 ('remote_dir', '/opt/../etc'), ('ssh_port', 0)]:
                invalid = {**config, field: value}
                path.write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_plan_rejects_unbounded_scope(self):
        plan = json.loads((Path(__file__).parent/'plan.example.json').read_text())
        self.assertEqual(validate_plan(plan), plan)
        plan['phases'][0]['rooms'] = 29
        with self.assertRaises(ValueError):
            validate_plan(plan)


if __name__ == '__main__':
    unittest.main()
