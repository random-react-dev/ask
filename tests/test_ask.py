"""Tests for the public-download launcher boundary."""
import hashlib
import io
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ask
from release_client import Client, ClientError, materialize


class LauncherTests(unittest.TestCase):
    def manifest(self):
        return {'launcher_protocol': 1, 'release': 'test', 'platform': sys.platform,
                'architecture': platform.machine(), 'runner': {'entrypoint': 'runner_cli.py'}}

    def test_forwards_question_and_returns_child_exit_code(self):
        client = Mock(request=Mock(return_value=self.manifest()))
        with patch.object(ask, 'Client', return_value=client), patch.object(ask, 'materialize', return_value=Path('/tmp/package')), patch.object(ask.subprocess, 'run', return_value=Mock(returncode=2)) as run, patch.object(sys, 'argv', ['ask.py', 'question']):
            self.assertEqual(ask.main(), 2)
        client.request.assert_called_once_with('/api/public/runner/releases/current')
        command = run.call_args.args[0]
        self.assertEqual(command[1], '/tmp/package/runner_cli.py')
        self.assertEqual(command[-2:], ['--', 'question'])
        self.assertEqual(run.call_args.kwargs, {})

    def test_forwards_resume_without_submitting_question(self):
        with patch.object(ask, 'Client', return_value=Mock(request=Mock(return_value=self.manifest()))), patch.object(ask, 'materialize', return_value=Path('/tmp/package')), patch.object(ask.subprocess, 'run', return_value=Mock(returncode=0)) as run, patch.object(sys, 'argv', ['ask.py', '--resume', 'task-id']):
            self.assertEqual(ask.main(), 0)
        self.assertEqual(run.call_args.args[0][-2:], ['--resume', 'task-id'])

    def test_incompatible_release_never_executes(self):
        manifest = self.manifest(); manifest['launcher_protocol'] = 999
        with patch.object(ask, 'Client', return_value=Mock(request=Mock(return_value=manifest))), patch.object(ask.subprocess, 'run') as run, patch.object(sys, 'argv', ['ask.py', 'question']):
            with self.assertRaises(ClientError): ask.main()
        run.assert_not_called()

    def test_download_client_rejects_nonpublic_routes(self):
        with self.assertRaises(ClientError):
            Client('https://example.com').request('/api/private/action')

    def test_help_requires_no_network(self):
        with patch.object(ask, 'Client') as client, patch.object(sys, 'argv', ['ask.py', '--help']):
            with self.assertRaises(SystemExit) as result: ask.main()
        self.assertEqual(result.exception.code, 0)
        client.assert_not_called()


class PackageTests(unittest.TestCase):
    def archive(self, name='runner_cli.py'):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive: archive.writestr(name, 'print(1)')
        data = stream.getvalue(); digest = hashlib.sha256(data).hexdigest()
        return data, {'sha256': digest, 'size': len(data), 'url': '/api/public/runner/artifacts/' + digest + '.zip', 'entrypoint': 'runner_cli.py'}

    def test_verified_cache_and_tamper_detection(self):
        data, descriptor = self.archive(); client = Mock(request=Mock(return_value=data))
        with tempfile.TemporaryDirectory() as directory:
            root = materialize(client, descriptor, directory)
            self.assertEqual(materialize(client, descriptor, directory), root)
            client.request.assert_called_once()
            (root / 'runner_cli.py').write_text('changed')
            with self.assertRaises(ClientError): materialize(client, descriptor, directory)

    def test_traversal_rejected(self):
        data, descriptor = self.archive('../runner_cli.py')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ClientError): materialize(Mock(request=Mock(return_value=data)), descriptor, directory)

    def test_checksum_rejected(self):
        data, descriptor = self.archive()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ClientError): materialize(Mock(request=Mock(return_value=data+b'x')), descriptor, directory)


if __name__ == '__main__': unittest.main()
