import json
import subprocess
import time


def invoke(command, request, diagnostic, recovered, run=subprocess.run, sleep=time.sleep):
    timeout = 230 if request['action'] == 'start' else 45
    for attempt in range(3):
        try:
            result = run(command, input=json.dumps(request), text=True, capture_output=True, timeout=timeout)
            code, stderr = result.returncode, result.stderr
        except subprocess.TimeoutExpired:
            code, stderr = 124, 'SSH deadline exceeded'
            result = None
        if code == 0:
            value = json.loads(result.stdout)
            if attempt:
                recovered(attempt)
            return value
        diagnostic(attempt, code, stderr)
        if code not in [255, 124] or attempt == 2:
            raise RuntimeError(f"Remote {request['action']} failed with exit {code} after {attempt + 1} attempts")
        sleep([2, 5][attempt])
