import json
import os
from pathlib import Path
import subprocess

SCRIPTS=Path(__file__).resolve().parent
ROOT=Path(os.environ.get('TM_LAB_STATE_DIR',str(SCRIPTS.parents[1]/'.my_local_dev/telemost'))).resolve()
progress=json.loads((ROOT/'progress.json').read_text()) if (ROOT/'progress.json').exists() else {}
state=json.loads((ROOT/'runner.private.json').read_text()) if (ROOT/'runner.private.json').exists() else {}
pid=state.get('pid')
command=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout if pid else ''
progress['runner_running']=str(SCRIPTS/'run.py') in command
progress['results']=json.loads((ROOT/'results.json').read_text()) if (ROOT/'results.json').exists() else []
if (ROOT/'cleanup.json').exists():
    progress['cleanup']=json.loads((ROOT/'cleanup.json').read_text())
print(json.dumps(progress,indent=2))
