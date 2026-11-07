import concurrent.futures
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from ssh_control import invoke
from lab_config import load_config, validate_plan

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
ROOT = Path(os.environ.get('TM_LAB_STATE_DIR', str(REPO/'.my_local_dev/telemost'))).resolve()
CONFIG = load_config(ROOT/'config.private.json')
REMOTE = CONFIG['remote_dir']
SSH_BASE = ['ssh', '-i', CONFIG['ssh_key'], '-p', str(CONFIG['ssh_port']), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
SSH = SSH_BASE + ['-o', 'ControlMaster=auto', '-o', 'ControlPersist=60', '-o', 'ControlPath='+os.path.relpath(ROOT/'ssh.sock', REPO)]
TARGET = CONFIG['ssh_target']
PLAN = validate_plan(json.loads((ROOT/'plan.json').read_text()))
GATE = 'http://127.0.0.1:18990'
REMOTE_PREPARED = False
RUN_ID = uuid.uuid4().hex
PROCESSES = []
WORKERS = []
FORWARD = None
FORWARD_RESTARTS = 0
RESULTS = []
STARTED = time.monotonic()
PHASE = None
CACHE = {}
os.umask(0o077)


def write(path, value):
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, indent=2)+'\n')
    temporary.replace(path)


def event(kind, **fields):
    row = {'utc': datetime.now(timezone.utc).isoformat(), 'kind': kind, **fields}
    with (ROOT/'events.jsonl').open('a') as stream:
        stream.write(json.dumps(row)+'\n')
    print(json.dumps(row), flush=True)


def remote(action, **fields):
    request = {'action': action, 'run_id': RUN_ID, **fields}
    def diagnostic(attempt, code, stderr):
        with (ROOT/'ssh-errors.private.jsonl').open('a') as stream:
            stream.write(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),'action':action,'attempt':attempt+1,'code':code,'stderr':stderr})+'\n')
        event('ssh_attempt_failed',action=action,attempt=attempt+1,code=code)
    result = invoke(SSH+[TARGET,'python3 '+shlex.quote(REMOTE+'/remote.py')],request,diagnostic,
                    lambda attempts:event('ssh_recovered',action=action,retries=attempts))
    if result.get('ok') is False:
        raise RuntimeError('Remote action rejected: '+result.get('reason','unknown'))
    return result

def snapshot():
    with urllib.request.urlopen(GATE+'/snapshot', timeout=5) as response:
        return json.load(response)


def deadline():
    if time.monotonic()-STARTED >= PLAN['maximum_duration_seconds']:
        raise TimeoutError('Experiment duration limit')


def sleep(seconds):
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        deadline()
        time.sleep(max(0,min(1, end-time.monotonic())))


def spawn(command, log, env=None, worker=False):
    with log.open('ab') as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    (WORKERS if worker else PROCESSES).append(process)
    write(ROOT/'processes.private.json', [{'pid': p.pid, 'active': p.poll() is None} for p in PROCESSES+WORKERS])
    return process


def terminate(processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def env_for(account, number, role, poll):
    env = os.environ.copy()
    for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
        env.pop(key, None)
    env.update(TM_API_GATE=GATE, TM_ACCOUNT=account, TM_EGRESS='mac', TM_ROLE=role,
               TM_ROOM=f'{PHASE}-{number:03}', TM_WAITING_POLL=f'{poll}s')
    return env


def probe(number, kind='health'):
    timeout = PLAN['health_timeout_seconds'] if kind == 'health' else PLAN['payload_timeout_seconds']
    command = [sys.executable, str(SCRIPTS/'bench.py'), kind, '--socks', str(18500+number), '--mib', '1', '--timeout', str(timeout)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout+15)
        row = json.loads(result.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        row = {'kind': kind, 'ok': False, 'error': 'probe_process_failed', 'utc': datetime.now(timezone.utc).isoformat()}
    row.update(room=number, phase=PHASE)
    return row


def probes(numbers, kind='health', readiness=False):
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8,len(numbers))) as pool:
        rows = list(pool.map(lambda n: probe(n, kind), numbers))
    with (ROOT/'probes.jsonl').open('a') as stream:
        for row in rows:
            row['readiness'] = readiness
            stream.write(json.dumps(row)+'\n')
    return rows


def resources(remote_numbers):
    pids = [p.pid for p in WORKERS if p.poll() is None]
    output = subprocess.check_output(['ps', '-o', 'pid=,rss=,%cpu=', '-p', ','.join(map(str,pids))], text=True) if pids else ''
    rows = [line.split() for line in output.splitlines() if len(line.split()) == 3]
    rss = sum(int(row[1])*1024 for row in rows)
    vm = subprocess.check_output(['vm_stat'], text=True)
    page = int(re.search(r'page size of (\d+) bytes',vm).group(1))
    pages = {name:int(value.strip().rstrip('.')) for name,value in (line.split(':',1) for line in vm.splitlines()[1:] if ':' in line) if value.strip().rstrip('.').isdigit()}
    available = sum(pages.get(key,0) for key in ['Pages free','Pages inactive','Pages speculative'])*page
    result = {'local_rss': rss, 'local_available_reclaimable': available, 'local_cpu_percent': sum(float(row[2]) for row in rows)}
    if rss > 3*1024**3 or available < 4*1024**3:
        raise RuntimeError('Local memory guard')
    if remote_numbers:
        result['vps'] = remote('status', numbers=remote_numbers)
        if result['vps']['available'] < 220*1024**2:
            raise RuntimeError('VPS memory guard')
        if any(unit.get('ActiveState') != 'active' for unit in result['vps']['units']):
            raise RuntimeError('VPS creator exited')
    ensure_support()
    if any(p.poll() is not None for p in WORKERS):
        raise RuntimeError('Local worker exited')
    return result


def start_room(config, number):
    account = 'a'
    key = f'a-{number:03}'
    room = CACHE.get(key)
    folder = ROOT/PHASE
    if number <= config['vps_rooms']:
        state = remote('start', number=number, phase=PHASE, poll=config['poll'], room=room)
        if not state.get('ok'):
            raise RuntimeError(state.get('reason','remote_start_failed'))
        room = state['room']
    else:
        link = folder/f'{number:03}.link.private'
        command = ['nice','-n','10',str(ROOT/'creator-mac'),'--cookies',str(ROOT/f'account-{account}.cookies.private.json'),
                   '--resources','moderate','--allow-loopback','--write-file',str(link)]
        if room:
            command.extend(['--tm-link',room])
        process = spawn(command,folder/f'{number:03}.creator.private.log',env_for(account,number,'creator',config['poll']),True)
        end = time.monotonic()+195
        while time.monotonic()<end:
            deadline()
            if process.poll() is not None:
                raise RuntimeError('Creator exited before readiness')
            if link.exists() and link.stat().st_size:
                room=link.read_text().strip().splitlines()[-1]
                break
            time.sleep(.5)
        else:
            raise TimeoutError('Creator readiness deadline')
    if not re.fullmatch(r'https://telemost\.yandex\.ru/j/\d+',room):
        raise RuntimeError('Invalid conference link')
    CACHE[key]=room
    write(ROOT/'rooms.private.json',CACHE)
    command=['nice','-n','10',str(ROOT/'joiner-mac'),'--tm-link',room,'--socks-host','127.0.0.1',
             '--socks-port',str(18500+number),'--reliable','--vp8-fps','24','--vp8-batch','60','--resources','moderate']
    process=spawn(command,folder/f'{number:03}.joiner.private.log',env_for(account,number,'joiner',config['poll']),True)
    readiness_deadline=time.monotonic()+190
    while time.monotonic()<readiness_deadline:
        sleep(2)
        row=probes([number],readiness=True)[0]
        if row['ok']:
            event('room_ready',phase=PHASE,room=number,account=account,creator_egress='vps' if number<=config['vps_rooms'] else 'mac')
            return
        if process.poll() is not None:
            break
    raise RuntimeError('Data path not ready')


def save_progress(**fields):
    write(ROOT/'progress.json',{'utc':datetime.now(timezone.utc).isoformat(),'phase':PHASE,'elapsed_seconds':time.monotonic()-STARTED,'completed_phases':len(RESULTS),**fields})


def phase(config):
    global PHASE
    PHASE=config['name']
    (ROOT/PHASE).mkdir(mode=0o700)
    initial=snapshot()
    before_429=initial['counts'].get('429',0)
    numbers=[]
    health=[]
    payload=[]
    failure=None
    hold_start=None
    ramp_complete=False
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pending=None
    event('phase_start',config=config)
    def payload_round():
        rows=[]
        for kind in ['download','upload']:
            rows.extend(probes(numbers,kind))
        return rows
    try:
        last_start=0
        for number in range(1,config['rooms']+1):
            ensure_support()
            sleep(max(0,PLAN['start_interval_seconds']-(time.monotonic()-last_start)))
            last_start=time.monotonic()
            start_room(config,number)
            numbers.append(number)
            if number%5==0:
                rows=probes(numbers,readiness=True)
                if any(not row['ok'] for row in rows):
                    event('ramp_health_failure',phase=PHASE,failed=sum(not row['ok'] for row in rows))
                resources([n for n in numbers if n<=config['vps_rooms']])
            save_progress(state='ramping',rooms=len(numbers),target=config['rooms'],gate=snapshot())
            if snapshot()['counts'].get('429',0)>before_429:
                failure='ramp_frozen_after_429'
                break
        ramp_complete=len(numbers)==config['rooms'] and failure is None
        if not ramp_complete:
            raise RuntimeError(failure or 'Incomplete ramp')
        hold_start=time.monotonic()
        event('hold_start',phase=PHASE,rooms=len(numbers),requested_seconds=config['hold'],ramp_complete=True)
        failures={n:0 for n in numbers}
        last_payload=hold_start
        while time.monotonic()-hold_start<config['hold']:
            deadline()
            ensure_support()
            round_start=time.monotonic()
            gate=snapshot()
            if gate['counts'].get('429',0)-before_429>=3:
                failure='repeated_429'
                break
            rows=probes(numbers)
            health.extend(rows)
            for row in rows:
                failures[row['room']]=0 if row['ok'] else failures[row['room']]+1
            if max(failures.values(),default=0)>=3:
                failure='three_failed_health_rounds'
                break
            if pending is not None and pending.done():
                completed=pending.result()
                payload.extend(completed)
                pending=None
                failed=[row for row in completed if not row['ok']]
                if failed:
                    event('payload_errors',phase=PHASE,errors=failed)
                if any(not row['ok'] and 'error' not in row for row in completed):
                    failure='payload_integrity_failed'
                    break
            if pending is None and time.monotonic()-last_payload>=PLAN['payload_interval_seconds']:
                pending=pool.submit(payload_round)
                last_payload=time.monotonic()
            stats=resources([n for n in numbers if n<=config['vps_rooms']])
            save_progress(state='holding',rooms=len(numbers),hold_seconds=time.monotonic()-hold_start,hold_target=config['hold'],health_passed=sum(row['ok'] for row in health),health_total=len(health),payload_passed=sum(row['ok'] for row in payload),payload_total=len(payload),payload_inflight=pending is not None,gate=gate,resources=stats)
            with (ROOT/'resources.jsonl').open('a') as stream:
                stream.write(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),'phase':PHASE,**stats})+'\n')
            sleep(max(0,min(PLAN['health_interval_seconds']-(time.monotonic()-round_start),config['hold']-(time.monotonic()-hold_start))))
        if pending is not None:
            payload.extend(pending.result(timeout=180))
            pending=None
        if failure is None and ramp_complete:
            payload.extend(payload_round())
        if failure is None and any(not row['ok'] for row in payload):
            failure='payload_errors_recorded'
    except (RuntimeError,TimeoutError,subprocess.SubprocessError,OSError) as exc:
        failure=f'{type(exc).__name__}: {exc}'
        event('phase_failure',phase=PHASE,reason=failure)
    finally:
        pool.shutdown(wait=True,cancel_futures=True)
        if pending is not None:
            try:
                payload.extend(pending.result())
            except Exception as exc:
                event('payload_worker_error',error=type(exc).__name__)
        gate=snapshot()
        result={'config':config,'ready_rooms':len(numbers),'ramp_complete':ramp_complete,
                'hold_seconds':time.monotonic()-hold_start if hold_start else 0,
                'health_passed':sum(row['ok'] for row in health),'health_total':len(health),
                'payload_passed':sum(row['ok'] for row in payload),'payload_total':len(payload),
                'payload_over_30_seconds':sum(row.get('over_30_seconds',False) for row in payload),
                'api_429':gate['counts'].get('429',0)-before_429,
                'api_counts_delta':{k:v-initial['counts'].get(k,0) for k,v in gate['counts'].items()},'failure':failure}
        result['clean']=ramp_complete and result['hold_seconds']>=config['hold'] and failure is None and result['api_429']==0 and len(health)>0 and result['health_passed']==len(health) and len(payload)>=2*len(numbers) and result['payload_passed']==len(payload)
        terminate(WORKERS)
        WORKERS.clear()
        stopped=remote('stop')
        result['remote_stopped']=stopped['stopped']
        RESULTS.append(result)
        write(ROOT/'results.json',RESULTS)
        event('phase_finished',**result)
    return result

def cooldown(last):
    wait=PLAN['cooldown_after_429_seconds'] if last.get('api_429') else PLAN['cooldown_between_phases_seconds']
    event('cooldown',seconds=wait)
    save_progress(state='cooldown',seconds=wait)
    sleep(wait)


def forward():
    options=SSH_BASE+['-o','ControlMaster=no','-o','ControlPath=none','-o','ControlPersist=no','-o','ForkAfterAuthentication=no']
    return spawn(options+['-N','-o','ExitOnForwardFailure=yes','-R','127.0.0.1:18990:127.0.0.1:18990',TARGET],ROOT/'forward.private.log')


def ensure_support():
    global FORWARD, FORWARD_RESTARTS
    if FORWARD is not None and FORWARD.poll() is not None:
        if FORWARD_RESTARTS>=3:
            raise RuntimeError('SSH forwarding recovery budget exhausted')
        FORWARD_RESTARTS+=1
        event('ssh_forward_restart',attempt=FORWARD_RESTARTS)
        FORWARD=forward()
        sleep(2)
    if any(p.poll() is not None for p in PROCESSES if p is not FORWARD and p.args[-1] != TARGET):
        raise RuntimeError('Local support process failed')


def prepare():
    global FORWARD, REMOTE_PREPARED
    for port in [18093,18990,*range(18501,18529)]:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',port))
    if (ROOT/'events.jsonl').exists():
        raise RuntimeError('Existing run evidence')
    CACHE.update(json.loads((ROOT/'rooms.private.json').read_text()))
    if any(f'a-{n:03}' not in CACHE for n in range(1,29)):
        raise RuntimeError('Expected the existing 28 rooms')
    if any(not re.fullmatch(r'https://telemost\.yandex\.ru/j/\d+', CACHE[f'a-{n:03}']) for n in range(1,29)):
        raise RuntimeError('Invalid cached conference link')
    if any(not (ROOT/name).is_file() for name in ['creator-mac', 'joiner-mac', 'account-a.cookies.private.json']):
        raise RuntimeError('Missing local binaries or test session')
    REMOTE_PREPARED=True
    if not remote('prepare').get('ok'):
        raise RuntimeError('Remote preparation failed or another test is active')
    spawn([sys.executable,str(SCRIPTS/'gate.py'),'--root',str(ROOT),'--account-rpm',str(PLAN['gate_account_rpm']),'--ip-rpm',str(PLAN['gate_ip_rpm'])],ROOT/'gate.private.log')
    spawn([sys.executable,str(SCRIPTS/'sink.py')],ROOT/'sink.private.log')
    FORWARD=forward()
    sleep(2)
    ensure_support()
    snapshot()

def cleanup():
    terminate(WORKERS)
    result = {}
    if REMOTE_PREPARED:
        try:
            result=remote('cleanup')
        except Exception as exc:
            result={'remote_cleanup_error':str(exc)}
    terminate(PROCESSES)
    for path in ROOT.glob('account-*.cookies.private.json'):
        path.unlink()
    result.update(local_sessions_removed=not list(ROOT.glob('account-*.cookies.private.json')),
                  local_children_stopped=all(p.poll() is not None for p in PROCESSES+WORKERS))
    write(ROOT/'cleanup.json',result)
    event('cleanup',**result)

def main():
    final=None
    try:
        prepare()
        event('started',plan=PLAN)
        for config in PLAN['phases']:
            if RESULTS:
                cooldown(RESULTS[-1])
            final=phase(config)
            if not final['ramp_complete'] or final['failure'] not in [None,'payload_errors_recorded']:
                break
        save_progress(state='completed',long_test_passed=bool(final and final['config']['hold']==7200 and final['hold_seconds']>=7200 and final['clean']),final=final)
    except BaseException as exc:
        event('aborted',error=type(exc).__name__)
        save_progress(state='aborted',reason=type(exc).__name__)
        raise
    finally:
        cleanup()
        subprocess.run([sys.executable,str(SCRIPTS/'summarize.py'),'--state',str(ROOT)],timeout=90,check=False)

if __name__=='__main__':
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chdir(REPO)
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(143))
    lock=(ROOT/'run.lock').open('a')
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    if (ROOT/'events.jsonl').exists():
        raise SystemExit('Existing run evidence; refusing to replace the run')
    write(ROOT/'runner.private.json',{'pid':os.getpid(),'run_id':RUN_ID,'started_utc':datetime.now(timezone.utc).isoformat()})
    main()
