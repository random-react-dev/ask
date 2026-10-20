#!/usr/bin/env python3
"""Submit research through the server-managed runner and return its deliverables."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from release_client import Client, ClientError, atomic_json, materialize

SERVICE = 'https://34.14.204.251'
STATE_DIR = Path.home() / 'Library/Application Support/DeviceAgent'
REPORT_INSTRUCTIONS = '''Research the question using research_search and research_fetch. Cite factual claims with source URLs. Compare at least three sources for the main findings. Save report.md in the provided output folder, with findings, sources and what was not covered. Do not access unrelated files or change computer settings.'''


def ask_question(argv):
    question = argv[0].strip() if argv else (sys.stdin.read().strip() if not sys.stdin.isatty() else '')
    if not question:
        raise ClientError('Pass a question or pipe one through standard input.')
    if len(question.encode('utf-8')) > 2048:
        raise ClientError('The question exceeds 2,048 UTF-8 bytes. Use one focused topic.')
    return question


def choose_provider():
    return next((name for name in ('codex', 'claude') if shutil.which(name)), None)


def enroll():
    path = STATE_DIR / 'device.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    if config.get('device_id') and config.get('device_token'):
        client = Client(SERVICE, config['device_token'])
        client.request('/api/device/work')
        return config, client
    intent_path = STATE_DIR / 'bootstrap-intent.json'
    if intent_path.exists():
        intent = json.loads(intent_path.read_text())
    else:
        code = Client(SERVICE).request('/api/admin/enrollments', {'ttl_seconds': 600})['code']
        intent = {'code': code, 'device_id': str(uuid.uuid4()), 'enrollment_nonce': str(uuid.uuid4()), 'label': platform.node(), 'platform': sys.platform}
        atomic_json(intent_path, intent)
    response = Client(SERVICE).request('/api/enroll/redeem', intent, retry=True)
    config = {'server_url': SERVICE, 'state_dir': str(STATE_DIR), 'device_id': response['device_id'], 'device_token': response['device_token']}
    atomic_json(path, config)
    intent_path.unlink(missing_ok=True)
    return config, Client(SERVICE, config['device_token'])


def prepare(config, client, provider, pinned_release=None):
    manifest = client.request('/api/device/releases/' + (pinned_release or 'current'))
    if manifest.get('protocol') != 2 or manifest.get('platform') != sys.platform or manifest.get('architecture') != platform.machine():
        raise ClientError('No compatible runtime release is available for this computer.')
    with open(STATE_DIR / 'task-agent.lock', 'a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if config.get('runner_protocol') == 2:
                return config
            raise ClientError('This computer is busy. Retry after its current task finishes.') from None
        package = materialize(client, manifest['runtime'], STATE_DIR / 'releases')
        client.request('/api/device/runtime-ready', {'runtime_release': manifest['release']}, retry=True)
        config.update(runner_protocol=2, runtime_release=manifest['release'], runtime_entry=str(package / manifest['runtime']['entrypoint']), runner_provider=provider, model='deepseek-flash', codex_bin=shutil.which('codex'), claude_bin=shutil.which('claude'), max_job_seconds=21600, research_gateway_enabled=True)
        atomic_json(STATE_DIR / 'device.json', config)
    return config


def new_task(question, provider, client):
    path = STATE_DIR / 'pending-submission.json'
    payload = {'device_id': json.loads((STATE_DIR / 'device.json').read_text())['device_id'], 'title': ' '.join(question.split())[:80], 'prompt': question + '\n\n' + REPORT_INSTRUCTIONS, 'provider': provider, 'kind': 'research'}
    if path.exists():
        previous = json.loads(path.read_text())
        if any(previous.get(k) != v for k, v in payload.items()):
            raise ClientError('An earlier submission is unresolved. Repeat its question before submitting another.')
        payload = previous
    else:
        payload['idempotency_key'] = uuid.uuid4().hex
        atomic_json(path, payload)
    data = client.request('/api/admin/tasks', payload, retry=True)
    atomic_json(STATE_DIR / 'requests' / (data['task_id'] + '.json'), {'task_id': data['task_id'], 'idempotency_key': payload['idempotency_key']})
    path.unlink()
    return data['task_id']


def start_runtime(config):
    with open(STATE_DIR / 'client-runtime.log', 'ab') as log:
        return subprocess.Popen([sys.executable, config['runtime_entry'], '--config', str(STATE_DIR / 'device.json')], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def wait_for_report(task_id, client, timeout):
    deadline, seen = time.monotonic() + timeout, set()
    print('Task: ' + task_id, flush=True)
    while time.monotonic() < deadline:
        data = client.request('/api/admin/tasks/' + task_id)
        session = data.get('runner_session', {})
        if session.get('status') in ('blocked', 'cancelled'):
            raise ClientError('Runner needs attention. Open the dashboard for errors and Retry; resume with --resume ' + task_id)
        for event in data.get('events', []):
            text = event.get('message')
            if text and text not in seen:
                print(text, flush=True)
                seen.add(text)
        if data['state'] not in ('pending', 'running'):
            return data
        time.sleep(min(2, max(0, deadline-time.monotonic())))
    print('Still running. Resume with: python3 ' + str(Path(__file__).resolve()) + ' --resume ' + task_id)
    return None


def save_file(client, url, name, expected_size=None, digest=None):
    name = ''.join(c for c in name.replace('\\', '_').replace('/', '_') if ord(c) >= 32).strip(' ._') or 'report'
    data = client.request(url, raw=True)
    if expected_size is not None and len(data) != expected_size:
        raise ClientError('Downloaded file size mismatch')
    if digest and hashlib.sha256(data).hexdigest() != digest:
        raise ClientError('Downloaded file integrity mismatch')
    original, index = Path(name), 1
    while True:
        target = Path.cwd() / (original.name if index == 1 else original.stem + '_' + str(index) + original.suffix)
        try:
            with target.open('xb') as stream:
                stream.write(data)
            print('Saved: ' + str(target))
            return
        except FileExistsError:
            index += 1


def collect_files(task_id, client, device_id):
    cursor, seen = None, set()
    root = '/api/admin/devices/' + device_id
    while True:
        page = client.request(root + '/files' + ('?before=' + urllib.parse.quote(cursor, safe='') if cursor else ''))
        for item in page.get('files', []):
            if item.get('task_id') == task_id and item['id'] not in seen:
                save_file(client, root + '/files/' + item['id'], item.get('name') or 'report', item.get('size'), item.get('sha256'))
                seen.add(item['id'])
        next_cursor = page.get('next_cursor')
        if not next_cursor:
            break
        if next_cursor == cursor:
            raise ClientError('Invalid file pagination cursor')
        cursor = next_cursor


def collect_large_files(task_id, client, device_id, outputs):
    import tempfile
    root = '/api/admin/devices/' + device_id + '/uploads'
    wanted = {file['id']: file for file in outputs if file.get('delivery') == 'background'}
    if not wanted:
        return False
    uploads = {file['id']: file for file in client.request(root)['uploads']}
    pending = False
    for identifier, receipt in wanted.items():
        item = uploads.get(identifier)
        if not item or item['state'] != 'ready':
            pending = True
            continue
        name = Path(receipt['path'].replace('\\', '/')).name.strip(' ._') or 'download'
        fd, temp = tempfile.mkstemp(dir=Path.cwd(), prefix='.ask-download-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                client.download(root + '/' + identifier + '/download', stream, receipt['size'], receipt['sha256'])
            index = 1
            while True:
                target = Path.cwd() / (name if index == 1 else Path(name).stem + '_' + str(index) + Path(name).suffix)
                try:
                    os.link(temp, target)
                    print('Saved: ' + str(target))
                    break
                except FileExistsError:
                    index += 1
        finally:
            os.unlink(temp)
    return pending


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question', nargs='?')
    parser.add_argument('--resume', metavar='TASK_ID')
    args = parser.parse_args()
    if args.resume and args.question:
        parser.error('Use a question or --resume, not both')
    if sys.platform != 'darwin' or sys.version_info < (3, 10):
        raise ClientError('macOS and Python 3.10+ are required.')
    provider = choose_provider()
    if not provider:
        raise ClientError('Install Codex CLI or Claude Code first.')
    question = None if args.resume else ask_question([args.question] if args.question else [])
    try:
        timeout = int(os.environ.get('ASK_TIMEOUT_SECONDS', '3600'))
        if timeout <= 0:
            raise ValueError()
    except ValueError:
        raise ClientError('ASK_TIMEOUT_SECONDS must be a positive integer.') from None
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(STATE_DIR / 'client.lock', 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config, client = enroll()
        existing = client.request('/api/admin/tasks/' + args.resume) if args.resume else {}
        if existing and existing['device_id'] != config['device_id']:
            raise ClientError('This task belongs to another computer.')
        pinned = existing.get('runner_session', {}).get('runtime_release')
        config = prepare(config, client, provider, pinned)
        task_id = args.resume or new_task(question, provider, client)
        data = client.request('/api/admin/tasks/' + task_id)
        if data['device_id'] != config['device_id']:
            raise ClientError('This task belongs to another computer.')
        if data['state'] in ('pending', 'running'):
            start_runtime(config)
    data = wait_for_report(task_id, client, timeout)
    if data is None:
        return 2
    if data['state'] != 'completed':
        raise ClientError('Research ' + data['state'] + ': ' + str(data.get('error') or 'See the dashboard.'))
    print((data.get('result') or {}).get('text', ''))
    collect_files(task_id, client, config['device_id'])
    outputs = (data.get('result') or {}).get('outputs', [])
    pending = collect_large_files(task_id, client, config['device_id'], outputs)
    if pending or any(file.get('delivery') == 'blocked' for file in outputs):
        print('Some files require background delivery. Check delivery status in the dashboard; resume with --resume ' + task_id)
        return 2
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ClientError, OSError, ValueError, KeyError) as exc:
        print('Ask: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
