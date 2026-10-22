#!/usr/bin/env python3
"""Research a question and return a sourced report."""
import argparse
from pathlib import Path
import platform
import subprocess
import sys
from release_client import Client, ClientError, materialize

SERVICE = 'https://ask-gateway.r95426882.workers.dev'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question', nargs='?')
    parser.add_argument('--resume', metavar='TASK_ID')
    args = parser.parse_args()
    if args.question is not None and args.resume:
        parser.error('Use a question or --resume, not both')
    client = Client(SERVICE)
    manifest = client.request('/api/public/runner/releases/current')
    if manifest.get('launcher_protocol') != 1:
        raise ClientError('Please update this skill to use the available runner.')
    if manifest.get('platform') != sys.platform or manifest.get('architecture') != platform.machine():
        raise ClientError('No compatible runner is available for this computer.')
    package = materialize(client, manifest['runner'], Path.home() / 'Library/Caches/ask-runner')
    command = [sys.executable, str(package / manifest['runner']['entrypoint']),
               '--service', SERVICE, '--release', manifest['release'],
               '--launcher', str(Path(__file__).resolve())]
    if args.resume:
        command.extend(['--resume', args.resume])
    elif args.question is not None:
        command.extend(['--', args.question])
    # Inherit stdin, stdout, stderr and cwd so questions and reports pass through unchanged.
    return subprocess.run(command).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ClientError, OSError, ValueError, KeyError) as exc:
        print('Ask: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
