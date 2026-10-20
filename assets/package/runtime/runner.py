#!/usr/bin/env python3
"""Register first, then run a real model-backed setup agent and exit."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import stat
import sys
import time
import uuid
from common import API, ModelRelay, ROOT, atomic_json, read_config, run_cli
from outputs import CAPABILITIES, RUNTIME_VERSION


def stage_runtime(source, state):
    """Prepare the whole code directory before replacing the managed runtime."""
    source, state = Path(source), Path(state)
    required = ('common.py', 'runner.py', 'setup_step.py', 'task_agent.py', 'outputs.py', 'models.json')
    if any(not (source / name).is_file() for name in required):
        raise RuntimeError('Runtime bundle is incomplete; installed runtime retained')
    runtime = state / 'runtime'
    if runtime.is_symlink():
        raise RuntimeError('Managed runtime destination must not be a symlink')
    files = {path.name: path.read_bytes() for path in source.glob('*.py')}
    files['models.json'] = (source / 'models.json').read_bytes()
    staging = state / ('.runtime-stage-' + uuid.uuid4().hex)
    previous = state / ('.runtime-previous-' + uuid.uuid4().hex)
    staging.mkdir(mode=0o700)
    moved_previous = False
    try:
        for name, data in files.items():
            fd = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
        if runtime.exists():
            os.rename(runtime, previous)
            moved_previous = True
        try:
            os.rename(staging, runtime)
        except OSError:
            if moved_previous:
                os.rename(previous, runtime)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if moved_previous:
        with contextlib.suppress(OSError):
            shutil.rmtree(previous)
    return runtime


def refresh_completed_runtime(config, api, source=ROOT):
    """Refresh an idle registered device without rerunning setup or inference."""
    state = Path(config['state_dir'])
    fd = os.open(state / 'task-agent.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError('Runtime refresh deferred: worker lock is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Runtime refresh deferred: a task agent is active; try again when idle') from None
        ready = api.request('/api/device/work')
        if not ready.get('setup_complete'):
            return False
        if ready.get('active_worker'):
            raise RuntimeError('Runtime refresh deferred: the server has an active task attempt')
        stage_runtime(source, state)
        atomic_json(state / 'runtime-refresh.json', {'runtime_version': RUNTIME_VERSION, 'capabilities': CAPABILITIES, 'refreshed_at': time.time(), 'existing_setup_retained': True})
        print('Runtime refreshed; existing setup retained.', flush=True)
        return True
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, help='Private bootstrap JSON: HTTPS origin, enrollment code and installation paths')
    args = parser.parse_args()
    bootstrap = read_config(args.config)
    state = Path(bootstrap['state_dir'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    registration = state / 'enrollment-intent.json'
    if registration.exists():
        intent = json.loads(registration.read_text())
    else:
        intent = {'device_id': bootstrap.get('device_id', str(uuid.uuid4())), 'enrollment_nonce': str(uuid.uuid4())}
        atomic_json(registration, intent)
    device_path = state / 'device.json'
    if device_path.exists():
        config = read_config(device_path)
    else:
        # This is deliberately the first remote operation. No CLI or helper starts earlier.
        response = API(bootstrap).request('/api/enroll/redeem', {**intent, 'code': bootstrap['enrollment_code'], 'label': bootstrap.get('label', platform.node()), 'platform': sys.platform}, authenticated=False)
        config = {k: v for k, v in bootstrap.items() if k not in ('enrollment_code', 'enrollment_nonce')}
        config.update(device_id=response['device_id'], device_token=response['device_token'])
        config['setup_manifest'] = response.get('setup_manifest')
        atomic_json(device_path, config)
    api = API(config)
    manifest = api.request('/api/device/setup/manifest')
    expected_steps = ['start-task-agent', 'install-helper', 'verify-helper']
    if manifest.get('version') != 1 or manifest.get('steps') != expected_steps:
        raise RuntimeError('server setup manifest is incompatible; refusing unknown commands')
    config['setup_manifest'] = manifest
    if refresh_completed_runtime(config, api):
        return 0
    runtime = stage_runtime(ROOT, state)
    bundled_helper = ROOT.parent / 'helper'
    staged_helper = state / 'helper'
    for relative in ('install.py', 'target/release/device-helper'):
        source = bundled_helper / relative
        destination = staged_helper / relative
        if source.is_file() and source.resolve() != destination.resolve():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, destination)
    if not (staged_helper / 'install.py').is_file() or not (staged_helper / 'target/release/device-helper').is_file():
        raise RuntimeError('runner package is missing the built helper installer/binary')
    config['helper_install_command'] = [str(Path(sys.executable).resolve()), str(staged_helper / 'install.py')]
    config['task_agent_command'] = [str(Path(sys.executable).resolve()), str(runtime / 'task_agent.py')]
    atomic_json(device_path, config)
    api = API(config)
    api.request('/api/device/setup', {'stage': 'runner', 'status': 'running', 'detail': 'Registered; starting isolated model-backed setup runner'})
    (state / 'setup-complete.json').unlink(missing_ok=True)
    command = [str(Path(sys.executable).resolve()), str(runtime / 'setup_step.py'), '--config', str(device_path)]
    import shlex
    prefix = shlex.join(command)
    prompt = ('You are the setup runner for this registered computer. Perform only these fixed setup steps in order using the shell tool. '
              'Do not inspect credentials, change permissions, operate any UI, add software, or run computer-control tools. '
              'If a step fails, stop and report the failure. Run each command once; each is idempotent.\n'
              '1. ' + prefix + ' start-task-agent\n'
              '2. ' + prefix + ' install-helper\n'
              '3. ' + prefix + ' verify-helper\n'
              'After successful verification, report setup complete and exit. The task agent and helper must survive your exit.')
    try:
        runner_dir = state / 'runner' / str(uuid.uuid4())
        with ModelRelay(api, config.get('runner_provider', 'codex')) as relay:
            result = run_cli(config, config.get('runner_provider', 'codex'), runner_dir, relay, prompt)
        subprocess.run(command + ['verify-helper'], check=True, timeout=45, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        verified = json.loads((state / 'setup-steps.json').read_text())
        if not verified.get('verify-helper'):
            raise RuntimeError('runner exited without verified helper setup')
        result['setup_verified'] = True
        atomic_json(state / 'runner-result.json', result)
        atomic_json(state / 'setup-complete.json', {'completed_at': time.time()})
        api.request('/api/device/setup', {'stage': 'runner', 'status': 'completed', 'detail': 'Model runner completed fixed steps; independent task agent and helper verified'})
    except Exception as exc:
        with contextlib.suppress(Exception):
            api.request('/api/device/setup', {'stage': 'runner', 'status': 'failed', 'detail': str(exc)[:500]})
        raise
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
