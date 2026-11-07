import json
from pathlib import Path
import re


def load_config(path):
    config = json.loads(Path(path).read_text())
    target = config.get('ssh_target', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+', target) or target.endswith('.invalid'):
        raise ValueError('Configure an explicit SSH test host')
    remote = config.get('remote_dir', '/opt/telemost-lab')
    if not re.fullmatch(r'/[A-Za-z0-9_/-]+', remote) or '..' in Path(remote).parts or len(Path(remote).parts) < 3:
        raise ValueError('Remote directory must be an absolute path without traversal')
    key = Path(config['ssh_key']).expanduser().resolve()
    if not key.is_file():
        raise ValueError('SSH identity file does not exist')
    port = config.get('ssh_port', 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Invalid SSH port')
    return {'ssh_target': target, 'ssh_key': str(key), 'ssh_port': port, 'remote_dir': remote}


def validate_plan(plan):
    if not 1 <= plan['maximum_duration_seconds'] <= 14400:
        raise ValueError('The experiment must finish within four hours')
    if not 1 <= plan['gate_account_rpm'] <= 180 or not 1 <= plan['gate_ip_rpm'] <= 180:
        raise ValueError('API rates must stay within the configured experiment bound')
    for phase in plan['phases']:
        if not re.fullmatch(r'[A-Za-z0-9-]+', phase['name']):
            raise ValueError('Invalid phase name')
        if not 1 <= phase['rooms'] <= 28 or not 0 <= phase['vps_rooms'] <= min(10, phase['rooms']):
            raise ValueError('The experiment supports at most 28 rooms and 10 VPS creators')
        if phase['accounts'] != 1 or not 1 <= phase['hold'] <= 7200:
            raise ValueError('Invalid phase scope')
        if not 5 <= phase['poll'] <= 300:
            raise ValueError('Invalid waiting-room poll interval')
    return plan
