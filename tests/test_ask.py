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
    def test_download_client_rejects_nonpublic_routes(self):
        for path in ('/api/admin/tasks', '/api/device/work', '/api/private/action'):
            with self.assertRaises(ClientError): Client('https://example.com').request(path)

    def test_status_and_help_require_no_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ask, 'STATE_DIR', Path(directory)), patch.object(ask, 'Client') as client:
            job = 'ask_' + 'a'*32
            ask.private_dir(ask.job_path(job))
            ask.write_json(ask.job_path(job)/'receipt.json', {'job_id':job,'status':'preparing'})
            with patch.object(sys,'argv',['ask.py','--status',job]): self.assertEqual(ask.main(),0)
            with patch.object(sys,'argv',['ask.py','--help']):
                with self.assertRaises(SystemExit) as result: ask.main()
            self.assertEqual(result.exception.code,0)
            client.assert_not_called()

    def test_protocol_one_never_executes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ask, 'STATE_DIR', Path(directory)):
            job='ask_'+'b'*32;ask.private_dir(ask.job_path(job))
            ask.write_json(ask.job_path(job)/'receipt.json',{'job_id':job,'status':'preparing'})
            with patch.object(ask,'Client',return_value=Mock(request=Mock(return_value={'launcher_protocol':1}))), patch.object(ask.subprocess,'run') as run:
                self.assertEqual(ask.coordinate(job),1)
            run.assert_not_called()
            self.assertEqual(ask.read_job(job)['status'],'failed')


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
