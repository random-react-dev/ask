"""Real launcher and runner processes against a local HTTP service; no AI calls."""
import concurrent.futures
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import zipfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
RUNTIME = Path(os.environ['ASK_TEST_RUNTIME']) if os.environ.get('ASK_TEST_RUNTIME') else None
pytestmark = pytest.mark.skipif(RUNTIME is None, reason='Set ASK_TEST_RUNTIME to the protocol-2 runtime source')


@pytest.fixture
def flow(tmp_path):
    state = tmp_path/'state'; state.mkdir(mode=0o700)
    workspace = tmp_path/'workspace'; workspace.mkdir()
    report = b'# Verified fixture report\nOnly the successful attempt.\n'
    evidence = {'tasks':{}, 'posts':0, 'executions':0, 'gets':0, 'lost_ack':False, 'stuck':False, 'interrupt':False, 'downloads':0, 'delay':2, 'post_sleep':0, 'busy':0, 'bad_checksum':False}
    guard = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,value,status=200):
            blob = value if isinstance(value,bytes) else json.dumps(value).encode()
            self.send_response(status);self.send_header('Content-Length',str(len(blob)));self.end_headers()
            try:self.wfile.write(blob)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with guard:
                if self.path=='/api/admin/tasks':
                    evidence['posts']+=1; key=body['idempotency_key']
                    assert body['device_id']=='device'
                    if key not in evidence['tasks']:
                        evidence['tasks'][key]={'id':'task-'+str(len(evidence['tasks'])+1),'payload':body,'started':None}
                    task=evidence['tasks'][key]
                    assert task['payload']==body
                    result={'task_id':task['id'],'state':'pending'}
                    lose=evidence['lost_ack']; evidence['lost_ack']=False
                elif self.path=='/api/test/execute':
                    evidence['executions']+=1
                    if evidence['busy']: evidence['busy']-=1
                    else:
                        for task in evidence['tasks'].values():
                            if task['started'] is None: task['started']=time.time();break
                    result={};lose=False
                else:raise AssertionError(self.path)
            if evidence['post_sleep'] and self.path=='/api/admin/tasks': time.sleep(evidence['post_sleep'])
            if lose:self.connection.shutdown(2);self.connection.close();return
            self.reply(result)
        def do_GET(self):
            if self.path=='/api/device/work':self.reply({'can_start':True});return
            if self.path.startswith('/api/admin/tasks/'):
                with guard:
                    evidence['gets']+=1
                    task=next(t.copy() for t in evidence['tasks'].values() if self.path.endswith(t['id']))
                    started=task['started']
                    status='pending' if started is None else 'completed' if not evidence['stuck'] and time.time()-started>evidence['delay'] else 'running'
                self.reply({'id':task['id'],'device_id':'device','state':status,'attempt_id':'successful-attempt',
                            'result':{'outputs':[{'id':'current-report','path':'report.md','size':len(report),'sha256':hashlib.sha256(report).hexdigest()}]}})
            elif self.path.endswith('/files/current-report'):
                evidence['downloads']+=1
                if evidence['interrupt']:
                    evidence['interrupt']=False
                    self.send_response(200);self.send_header('Content-Length',str(len(report)));self.end_headers()
                    self.wfile.write(report[:10]);self.connection.shutdown(2);self.connection.close()
                else:self.reply(b'X'+report[1:] if evidence['bad_checksum'] else report)
            else:raise AssertionError('Unexpected endpoint, including old attempts: '+self.path)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url='http://127.0.0.1:'+str(server.server_port)
    entry=f'''import runner_cli as r
Original=r.Client
class Local(Original):
 def __init__(self,origin,token=None):
  super().__init__(origin,token);self.origin={url!r}
r.Client=Local
r.choose_provider=lambda:'codex'
r.start_runtime=lambda config: Local(r.SERVICE).request('/api/test/execute',{{'device_id':config['device_id']}})
raise SystemExit(r.main())
'''
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:
        for name in ('runner_cli.py','release_client.py'):z.writestr(name,(RUNTIME/name).read_bytes())
        z.writestr('entry.py',entry)
    blob=archive.getvalue();(tmp_path/'release.zip').write_bytes(blob)
    wrapper=tmp_path/'ask.py'
    wrapper.write_text(f'''import sys,time,json,hashlib,platform
from pathlib import Path
sys.path.insert(0,{str(SCRIPTS)!r})
import ask
ask.STATE_DIR=Path({str(state)!r});ask.SCRIPT=Path(__file__).resolve()
class Public:
 def __init__(self,origin):pass
 def request(self,path,raw=False):
  assert path.startswith('/api/public/runner/')
  data=Path({str(tmp_path/'release.zip')!r}).read_bytes();sha=hashlib.sha256(data).hexdigest()
  if raw:return data
  return {{'launcher_protocol':2,'release':'fixture-2','platform':sys.platform,'architecture':platform.machine(),'runner':{{'entrypoint':'entry.py','sha256':sha,'size':len(data),'url':'/api/public/runner/artifacts/'+sha+'.zip'}}}}
ask.Client=Public
raise SystemExit(ask.main())
''')
    (state/'device.json').write_text(json.dumps({'device_id':'device','device_token':'fixture','server_url':'https://ask-gateway.r95426882.workers.dev','state_dir':str(state)}))
    (state/'device.json').chmod(0o600)
    def call(*args):
        before=time.monotonic();p=subprocess.run([sys.executable,str(wrapper),*args],cwd=workspace,capture_output=True,text=True,timeout=5)
        assert p.stdout,p.stderr
        evidence['last_stdout'] = p.stdout
        return json.loads(p.stdout),p.returncode,time.monotonic()-before
    def wait(job,status='ready',timeout=25):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            data=json.loads((state/'jobs'/job/'receipt.json').read_text())
            if data['status']==status:return data
            if data['status']=='failed':pytest.fail(str(data))
            time.sleep(.05)
        pytest.fail('Timed out: '+str(data))
    yield evidence,state,call,wait
    # End only fixture-owned coordination; independent real agents are never run.
    for p in (state/'jobs').glob('*/heartbeat.json'):
        try:os.kill(json.loads(p.read_text())['pid'],signal.SIGTERM)
        except ProcessLookupError:pass
    server.shutdown();server.server_close();thread.join()


def test_complete_flow_and_repeat(flow):
    e,state,call,wait=flow
    first,code,elapsed=call('question');job=first['job_id'];assert code==0 and elapsed<2
    pending,code,timing=call('--status',job);assert code==0 and timing<5
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        repeated=list(pool.map(lambda _:call('question')[0]['job_id'],range(3)))
    assert repeated==[job]*3
    wait(job)
    result,code,duration=call('--result',job);assert code==0 and duration<5
    path=Path(result['report_path']);assert path.name=='report.md' and path.is_absolute()
    expected = path.read_text()
    assert result['report'] == expected and result['report_truncated'] is False
    assert e['last_stdout'].count(json.dumps(expected)[1:-1]) == 1
    before=path.stat().st_mtime_ns
    again,code,t2=call('--result',job)
    assert again['report'] == expected and again['report_truncated'] is False
    assert e['last_stdout'].count(json.dumps(expected)[1:-1]) == 1
    assert again['deliverables']==result['deliverables'] and path.stat().st_mtime_ns==before
    assert len(list(path.parent.iterdir()))==1 and len(e['tasks'])==1 and e['posts']==1
    print(f'B a/c/d: start={elapsed:.3f}s status={timing:.3f}s result={duration:.3f}s repeat={t2:.3f}s; task_count=1; posts=1; identical report; stdout text once, untruncated: PASS')


def test_killed_coordinator_recovers_same_task(flow):
    e,state,call,wait=flow;e['delay']=5
    first,_,_=call('recover');job=first['job_id'];folder=state/'jobs'/job
    end=time.monotonic()+10
    while time.monotonic()<end:
        current=json.loads((folder/'receipt.json').read_text())
        if current.get('task_id'):break
        time.sleep(.05)
    old=json.loads((folder/'heartbeat.json').read_text())['pid'];os.kill(old,signal.SIGKILL)
    time.sleep(.1)
    again,code,_=call('recover');assert again['job_id']==job and code==0
    finished=wait(job)
    assert finished['task_id']==current['task_id'] and len(e['tasks'])==1 and e['posts']==1
    assert call('--result',job)[1]==0
    print('B b: killed coordinator; repeated question; same task; task_count=1; report delivered: PASS')


def test_lost_ack_and_interrupted_download(flow):
    e,state,call,wait=flow;e['lost_ack']=True;e['interrupt']=True
    first,_,_=call('lost ack');job=first['job_id'];wait(job)
    assert len(e['tasks'])==1 and e['posts']>=2 and e['downloads']==2
    assert call('--result',job)[1]==0
    assert not list((state/'jobs'/job/'results').glob('.download-*'))
    print(f'B recovery: lost acknowledgement posts={e["posts"]}, task_count=1; interrupted download retried atomically: PASS')


def test_deadline_reattaches_without_resubmission(flow):
    e,state,call,wait=flow;e['stuck']=True
    first,_,_=call('--timeout-seconds','2','stuck');job=first['job_id'];time.sleep(2.3)
    failed,code,_=call('--status',job)
    assert code==1 and '--result '+job in failed['next_command'] and 'may still be running' in failed['message']
    assert '--new' in failed['new_request_fallback']
    original=json.loads((state/'jobs'/job/'receipt.json').read_text())['deadline_at']
    e['stuck']=False;e['delay']=0
    call('--result',job)
    wait(job)
    result,code,_=call('--result',job)
    assert code==0 and len(e['tasks'])==1 and e['posts']==1
    assert json.loads((state/'jobs'/job/'receipt.json').read_text())['deadline_at']==original
    print('B e: deadline failed; same-job result recovery; unchanged deadline; task_count=1: PASS')


def test_orphan_runner_serializes_recovery(flow):
    e,state,call,wait=flow;e['post_sleep']=3
    first,_,_=call('orphan');job=first['job_id'];folder=state/'jobs'/job
    end=time.monotonic()+10
    while not e['posts'] and time.monotonic()<end:time.sleep(.02)
    assert e['posts']==1
    os.kill(json.loads((folder/'heartbeat.json').read_text())['pid'],signal.SIGKILL)
    time.sleep(.1)
    assert call('orphan')[0]['job_id']==job
    wait(job)
    assert e['posts']==1 and len(e['tasks'])==1
    print('B orphan runner: coordinator killed during POST; recovery serialized; one POST/task: PASS')


def test_busy_execution_and_pending_checks_never_resubmit(flow):
    e,state,call,wait=flow;e['busy']=1;e['delay']=3
    first,_,_=call('busy');job=first['job_id']
    for _ in range(3):
        assert call('--status',job)[1]==0
        time.sleep(.2)
    wait(job)
    assert e['executions']==2 and e['posts']==1
    print('B busy execution: queued task retried after availability; status polls did not submit: PASS')


def test_checksum_failure_never_publishes_partial_report(flow):
    e,state,call,wait=flow;e['bad_checksum']=True
    first,_,_=call('checksum');job=first['job_id'];wait(job,status='failed')
    data,code,_=call('--status',job)
    assert code==1 and 'integrity' in data['error']
    assert not (state/'jobs'/job/'results'/'report.md').exists()
    assert not list((state/'jobs'/job/'results').glob('.download-*'))
    print('B checksum mismatch: failed with actual cause; no partial report: PASS')
