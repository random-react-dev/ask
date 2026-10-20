#!/usr/bin/env python3
"""Small ordered, idempotent commands called by the actual setup runner model."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
from common import API, atomic_json, read_config


def fresh_status(path, seconds=30):
    try:
        status = json.loads(Path(path).read_text())
        stamp = status.get('last_heartbeat', status.get('updated_at', 0))
        if not stamp or not (0 <= time.time() - float(stamp) < seconds):
            return False
        os.kill(int(status['pid']), 0)
        with open(Path(path).parent / 'task-agent.lock', 'a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def remote_fresh(remote, key, maximum_age):
    try:
        age = float(remote['server_time']) - float(remote[key])
        return 0 <= age <= maximum_age
    except (KeyError, ValueError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('step', choices=('start-task-agent', 'install-helper', 'verify-helper'))
    args = parser.parse_args()
    config = read_config(args.config)
    state = Path(config['state_dir'])
    steps_path = state / 'setup-steps.json'
    with open(state / 'setup-steps.lock', 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        steps = json.loads(steps_path.read_text()) if steps_path.exists() else {}
        api = API(config)
        if args.step == 'start-task-agent':
            remote = api.request('/api/device/work')
            if fresh_status(state / 'task-agent-status.json') and remote_fresh(remote, 'task_agent_last_seen', 30):
                steps[args.step] = time.time()
                atomic_json(steps_path, steps)
                api.request('/api/device/setup', {'stage': args.step, 'status': 'completed'})
                print(args.step + ': current authenticated agent reverified')
                return 0
        api.request('/api/device/setup', {'stage': args.step, 'status': 'running'})
        try:
            if args.step == 'start-task-agent':
                log_path = state / 'task-agent.log'
                fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, 'ab') as log:
                    process = subprocess.Popen(config['task_agent_command'] + ['--config', str(Path(args.config).resolve()), '--setup'], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
                atomic_json(state / 'task-agent-start.json', {'pid': process.pid, 'started_at': time.time(), 'parent_setup_step_pid': os.getpid()})
                end = time.monotonic() + 40
                while time.monotonic() < end:
                    if fresh_status(state / 'task-agent-status.json'):
                        remote = api.request('/api/device/work')
                        if remote_fresh(remote, 'task_agent_last_seen', 30):
                            break
                    if process.poll() is not None:
                        raise RuntimeError('independent task agent exited before first authenticated heartbeat')
                    time.sleep(0.5)
                else:
                    raise RuntimeError('independent task agent heartbeat timed out')
            elif args.step == 'install-helper':
                if not steps.get('start-task-agent'):
                    raise RuntimeError('start-task-agent must complete first')
                command = config.get('helper_install_command')
                if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
                    raise RuntimeError('operator must configure helper_install_command argv')
                subprocess.run(command + ['--config', str(Path(args.config).resolve())], check=True, timeout=120, stdin=subprocess.DEVNULL)
            else:
                if not steps.get('install-helper'):
                    raise RuntimeError('install-helper must complete first')
                command = config['helper_install_command']
                subprocess.run(command + ['--status', '--json'], check=True, timeout=30, stdin=subprocess.DEVNULL)
                if not fresh_status(state / 'task-agent-status.json'):
                    raise RuntimeError('task agent authenticated heartbeat is not fresh')
                # Installer verifies its local heartbeat and launchd job. Also require server view.
                remote = api.request('/api/device/work')
                if not remote_fresh(remote, 'helper_last_seen', 75):
                    raise RuntimeError('server has not observed a fresh helper heartbeat')
                if not remote_fresh(remote, 'task_agent_last_seen', 30):
                    raise RuntimeError('server has not observed a fresh task-agent heartbeat')
            steps[args.step] = time.time()
            atomic_json(steps_path, steps)
            api.request('/api/device/setup', {'stage': args.step, 'status': 'completed'})
            print(args.step + ': completed')
        except Exception as exc:
            with contextlib.suppress(Exception):
                api.request('/api/device/setup', {'stage': args.step, 'status': 'failed', 'detail': str(exc)[:500]})
            raise
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
