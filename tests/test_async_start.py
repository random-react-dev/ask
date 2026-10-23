"""Process-level acceptance checks with a verified fake release, never live research."""
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


@pytest.fixture
def fixture():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        state = root / 'state'
        workspace = root / 'workspace'; workspace.mkdir()
        runner = '''import argparse,json,pathlib,time
p=argparse.ArgumentParser();p.add_argument('--service');p.add_argument('--release');p.add_argument('--job');a=p.parse_args()
r=json.load(open(a.job));d=pathlib.Path(a.job).parent
assert r['idempotency_key'] and r['question_hash'] and r['workspace']
assert r['question_hash']==__import__('hashlib').sha256(r['question'].encode()).hexdigest()
with open(d/'called','w') as f: f.write(r['idempotency_key'])
(d/'called').chmod(0o600)
time.sleep(.2)
r['status']='ready'
(d/'next.json').write_text(json.dumps(r))
(d/'next.json').chmod(0o600)
(d/'next.json').replace(d/'receipt.json')
'''
        archive = io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z: z.writestr('runner.py',runner)
        blob=archive.getvalue(); (root/'release.zip').write_bytes(blob)
        wrapper=root/'entry.py'
        wrapper.write_text(f'''import sys,json,time,hashlib,platform
from pathlib import Path
sys.path.insert(0,{str(SCRIPTS)!r})
import ask
ask.STATE_DIR=Path({str(state)!r});ask.SCRIPT=Path(__file__).resolve()
if '--coordinate' in sys.argv:
 with open({str(root/'starts')!r},'a') as marker: marker.write(sys.argv[sys.argv.index('--coordinate')+1]+'\\n')
class FakeClient:
 def __init__(self,origin): pass
 def request(self,path,raw=False):
  assert path.startswith('/api/public/runner/')
  time.sleep(3)
  data=Path({str(root/'release.zip')!r}).read_bytes();sha=hashlib.sha256(data).hexdigest()
  if raw:return data
  return {{'launcher_protocol':2,'release':'test','platform':sys.platform,'architecture':platform.machine(),'runner':{{'entrypoint':'runner.py','sha256':sha,'size':len(data),'url':'/api/public/runner/artifacts/'+sha+'.zip'}}}}
ask.Client=FakeClient
raise SystemExit(ask.main())
''')
        def call(*args):
            started=time.monotonic()
            result=subprocess.run([sys.executable,str(wrapper),*args],cwd=workspace,capture_output=True,text=True,timeout=1.9)
            assert result.returncode==0,result.stderr
            return json.loads(result.stdout),time.monotonic()-started
        def finish(job):
            path=state/'jobs'/job/'receipt.json'; deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                data=json.loads(path.read_text())
                if data['status'] in ('ready','failed'):
                    assert data['status']=='ready',data
                    return data
                time.sleep(.05)
            pytest.fail('Fake runner did not finish')
        yield root,state,workspace,wrapper,call,finish
        # Tests wait for every created job; no process or state escapes this fixture.
        for path in (state/'jobs').glob('*/receipt.json'):
            finish(path.parent.name)


def test_fast_start_dedupe_permissions_and_status(fixture):
    root,state,workspace,wrapper,call,finish=fixture
    first,elapsed=call('question')
    assert elapsed<2
    job=first['job_id'];path=state/'jobs'/job
    assert '--status '+job in first['next_command']
    assert not (path/'called').exists()  # Fake network sleeps 3s twice.
    assert json.loads((path/'receipt.json').read_text())['idempotency_key']
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        repeated=list(pool.map(lambda _:call('question')[0],range(6)))
    assert {r['job_id'] for r in repeated}=={job}
    assert len(list((state/'jobs').iterdir()))==1
    data,status_elapsed=call('--status',job)
    assert status_elapsed<5 and data['status'] in ('preparing','running')
    finish(job)
    assert call('question')[0]['job_id']==job
    assert len(list((state/'jobs').glob('*/called')))==1
    assert (root/'starts').read_text().splitlines()==[job]
    for p in [state,state/'jobs',path]: assert stat.S_IMODE(p.stat().st_mode)==0o700
    for p in path.iterdir(): assert stat.S_IMODE(p.stat().st_mode)==0o600
    fresh,_=call('--new','question'); assert fresh['job_id']!=job
    finish(fresh['job_id'])
    print(f'A cold start: {elapsed:.3f}s with 6s simulated download; status: {status_elapsed:.3f}s; concurrent starts: 1 job/1 runner; --new: distinct; permissions: PASS')


def test_coordinator_survives_parent_group_termination(fixture):
    root,state,workspace,wrapper,call,finish=fixture
    parent=subprocess.Popen([sys.executable,'-c',
        'import subprocess,sys,time;subprocess.run([sys.executable,sys.argv[1],"survival"],check=True);time.sleep(20)',str(wrapper)],
        cwd=workspace,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    try:
        receipt=json.loads(parent.stdout.readline());job=receipt['job_id']
        os.killpg(parent.pid,signal.SIGTERM)
        parent.wait(timeout=2)
        finish(job)
        assert (state/'jobs'/job/'called').is_file()
        print('A process-group termination: caller stopped during slow download; independent runner finished: PASS')
    finally:
        if parent.poll() is None:os.killpg(parent.pid,signal.SIGKILL);parent.wait()
        parent.stdout.close();parent.stderr.close()
