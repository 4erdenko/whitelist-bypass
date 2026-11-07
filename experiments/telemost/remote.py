import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
SESSION = ROOT/'session.private'
PREFIX = 'telemost-lab-'
SLICE = 'telemost-lab.slice'
os.umask(0o077)


def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def available():
    return next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))


def states(numbers):
    rows = []
    for number in numbers:
        output = run(['systemctl', 'show', f'{PREFIX}{number:03}.service', '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'CPUUsageNSec', '-p', 'MainPID', '-p', 'ExecMainStatus'])
        row = dict(line.split('=', 1) for line in output.stdout.splitlines() if '=' in line)
        rows.append({'number': number, **row})
    return rows


def main(request):
    action = request['action']
    run_id = request.get('run_id', '')
    if not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise ValueError('A run identifier is required')
    owner = ROOT/'owner.private.json'
    if owner.exists() and json.loads(owner.read_text())['run_id'] != run_id:
        return {'ok': False, 'reason': 'another_test_owns_the_host'}
    if action != 'prepare' and not owner.exists():
        return {'ok': False, 'reason': 'no_test_owner'}
    if action == 'prepare':
        if not owner.exists():
            busy = any(row.get('ActiveState') in ['active', 'activating'] for row in states(range(1, 11)))
            busy = busy or any(run(['systemctl', 'is-active', unit]).returncode == 0
                               for unit in ['telemost-lab-sink.service', 'telemost-lab-guard.timer'])
            if busy:
                return {'ok': False, 'reason': 'another_test_is_active'}
        if not (SESSION/'account-a.cookies.private.json').is_file():
            return {'ok': False, 'reason': 'missing_test_session'}
        if not owner.exists():
            try:
                with owner.open('x') as stream:
                    json.dump({'run_id': run_id}, stream)
            except FileExistsError:
                return {'ok': False, 'reason': 'another_test_claimed_the_host'}
        sink_active = run(['systemctl', 'is-active', 'telemost-lab-sink.service']).returncode == 0
        sink = None if sink_active else run(['systemd-run', '--unit=telemost-lab-sink', '--collect',
                    '--property=RuntimeMaxSec=5h', '--property=MemoryMax=96M',
                    '--property=NoNewPrivileges=yes',
                    f'--property=StandardOutput=append:{ROOT}/sink.private.log',
                    '--property=StandardError=inherit', sys.executable, str(ROOT/'sink.py')])
        if sink is not None and sink.returncode:
            return {'ok': False, 'reason': 'sink_start_failed'}
        guard_active = run(['systemctl', 'is-active', 'telemost-lab-guard.timer']).returncode == 0
        guard = None if guard_active else run(['systemd-run', '--unit=telemost-lab-guard', '--on-active=5h',
                     '--timer-property=AccuracySec=10s', sys.executable, str(ROOT/'remote.py'), 'cleanup', run_id])
        if guard is not None and guard.returncode:
            run(['systemctl', 'stop', 'telemost-lab-sink.service'])
            return {'ok': False, 'reason': 'guard_start_failed'}
        return {'ok': True}
    if action == 'start':
        n = int(request['number'])
        if not 1 <= n <= 10 or available() < 350*1024**2:
            return {'ok': False, 'reason': 'resource_guard'}
        phase = request['phase']
        if not phase.replace('-', '').isalnum():
            raise ValueError('Invalid phase')
        folder = ROOT / phase
        folder.mkdir(mode=0o700, exist_ok=True)
        link = folder / f'{n:03}.link.private'
        if link.exists():
            if states([n])[0].get('ActiveState') == 'active':
                return {'ok': True, 'room': link.read_text().strip().splitlines()[-1], 'reconciled': True}
            raise ValueError('Existing phase output without an active worker')
        log = folder / f'{n:03}.creator.private.log'
        command = ['systemd-run', f'--unit={PREFIX}{n:03}', '--collect', f'--slice={SLICE}',
                   '--property=RuntimeMaxSec=6h', '--property=MemoryMax=96M', '--property=NoNewPrivileges=yes',
                   '--property=ProtectSystem=strict', f'--property=ReadWritePaths={ROOT}', '--property=PrivateTmp=yes',
                   f'--property=StandardOutput=append:{log}', '--property=StandardError=inherit',
                   '--setenv=TM_API_GATE=http://127.0.0.1:18990', '--setenv=TM_ACCOUNT=a', '--setenv=TM_EGRESS=vps',
                   '--setenv=TM_ROLE=creator', f'--setenv=TM_ROOM={phase}-{n:03}', f'--setenv=TM_WAITING_POLL={int(request["poll"])}s',
                   str(ROOT/'creator-linux'), '--cookies', str(SESSION/'account-a.cookies.private.json'),
                   '--resources', 'moderate', '--allow-loopback', '--write-file', str(link)]
        if request.get('room'):
            command.extend(['--tm-link', request['room']])
        existing = states([n])[0].get('ActiveState') in ['active', 'activating']
        launch = None if existing else run(command)
        if launch is not None and launch.returncode:
            return {'ok': False, 'reason': 'launch_failed'}
        cap = run(['systemctl', 'set-property', '--runtime', SLICE, 'MemoryMax=400M', 'CPUQuota=100%'])
        if cap.returncode:
            run(['systemctl', 'stop', f'{PREFIX}{n:03}'])
            return {'ok': False, 'reason': 'resource_cap_failed'}
        deadline = time.monotonic()+190
        while time.monotonic() < deadline:
            if link.exists() and link.stat().st_size:
                return {'ok': True, 'room': link.read_text().strip().splitlines()[-1]}
            if states([n])[0].get('ActiveState') not in ['active', 'activating']:
                return {'ok': False, 'reason': 'creator_exited'}
            time.sleep(.5)
        return {'ok': False, 'reason': 'creator_timeout'}
    if action == 'status':
        cap = run(['systemctl', 'show', SLICE, '-p', 'MemoryCurrent', '-p', 'MemoryPeak', '-p', 'ControlGroup'])
        fields = dict(line.split('=', 1) for line in cap.stdout.splitlines() if '=' in line)
        cpu = Path('/sys/fs/cgroup') / fields.get('ControlGroup', '').lstrip('/') / 'cpu.stat'
        return {'available': available(), 'slice': fields, 'cpu': dict(line.split() for line in cpu.read_text().splitlines()) if cpu.exists() else {}, 'units': states(request.get('numbers', []))}
    if action in ['stop', 'cleanup']:
        run(['systemctl', 'stop', *[f'{PREFIX}{n:03}.service' for n in range(1,11)]])
        rows = states(range(1,11))
        if action == 'cleanup':
            run(['systemctl', 'stop', 'telemost-lab-sink.service'])
            run(['systemctl', 'stop', 'telemost-lab-guard.timer'])
            for path in SESSION.glob('*.json'):
                path.unlink()
            run(['systemctl', 'stop', SLICE])
            stopped = all(row.get('ActiveState') not in ['active', 'activating', 'deactivating'] for row in rows)
            stopped = stopped and all(run(['systemctl', 'is-active', unit]).returncode != 0
                                      for unit in ['telemost-lab-sink.service', 'telemost-lab-guard.timer'])
            if stopped:
                owner.unlink()
            return {'stopped': stopped, 'session_absent': not list(SESSION.glob('*.json'))}
        return {'stopped': all(row.get('ActiveState') != 'active' for row in rows)}
    raise ValueError('Invalid action')


if __name__ == '__main__':
    request = {'action': 'cleanup', 'run_id': sys.argv[2]} if len(sys.argv) == 3 and sys.argv[1] == 'cleanup' else json.load(sys.stdin)
    print(json.dumps(main(request)))
