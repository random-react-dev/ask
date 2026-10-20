"""Boundary tests: no installed runtime or live service required."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ask
from release_client import ClientError, materialize


class ClientTests(unittest.TestCase):
    def test_question_utf8_limit(self):
        self.assertEqual(ask.ask_question([' hello ']), 'hello')
        with self.assertRaises(ClientError): ask.ask_question(['é' * 1025])

    def test_ambiguous_submission_reuses_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(ask, 'STATE_DIR', Path(directory)):
            (Path(directory) / 'device.json').write_text(json.dumps({'device_id': 'device'}))
            client = Mock(); client.request.side_effect = [ClientError('lost response'), {'task_id': 'task'}]
            with self.assertRaises(ClientError): ask.new_task('question', 'codex', client)
            first = json.loads((Path(directory) / 'pending-submission.json').read_text())['idempotency_key']
            self.assertEqual(ask.new_task('question', 'codex', client), 'task')
            self.assertEqual(client.request.call_args.args[1]['idempotency_key'], first)

    def test_pagination_beyond_three_pages(self):
        client = Mock(); client.request.side_effect = [{'files': [], 'next_cursor': str(i)} for i in range(4)] + [{'files': [], 'next_cursor': None}]
        ask.collect_files('task', client, 'device')
        self.assertEqual(client.request.call_count, 5)

    def test_collision_keeps_original_stem(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                Path('report.md').write_text('original'); Path('report_2.md').write_text('second')
                ask.save_file(Mock(request=Mock(return_value=b'new')), '/api/file', 'report.md', 3)
                self.assertEqual(Path('report_3.md').read_bytes(), b'new')
                self.assertEqual(Path('report.md').read_text(), 'original')
            finally: os.chdir(previous)

    def test_timeout_does_not_create_task(self):
        client = Mock()
        with patch('ask.time.monotonic', side_effect=[0, 2]):
            self.assertIsNone(ask.wait_for_report('task', client, 1))
        client.request.assert_not_called()

    def test_blocked_runner_reported(self):
        client = Mock(request=Mock(return_value={'state': 'pending', 'runner_session': {'status': 'blocked'}}))
        with self.assertRaisesRegex(ClientError, 'needs attention'):
            ask.wait_for_report('task', client, 10)


class PackageTests(unittest.TestCase):
    def archive(self, name='entry.py'):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive: archive.writestr(name, 'print(1)')
        data = stream.getvalue(); digest = hashlib.sha256(data).hexdigest()
        return data, {'sha256': digest, 'size': len(data), 'url': '/api/device/artifacts/' + digest + '.zip', 'entrypoint': 'entry.py'}

    def test_verified_cache_and_tamper_detection(self):
        data, descriptor = self.archive(); client = Mock(request=Mock(return_value=data))
        with tempfile.TemporaryDirectory() as directory:
            root = materialize(client, descriptor, directory)
            self.assertEqual(materialize(client, descriptor, directory), root)
            client.request.assert_called_once()
            (root / 'entry.py').write_text('changed')
            with self.assertRaises(ClientError): materialize(client, descriptor, directory)

    def test_traversal_rejected(self):
        data, descriptor = self.archive('../entry.py')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ClientError): materialize(Mock(request=Mock(return_value=data)), descriptor, directory)

    def test_checksum_rejected(self):
        data, descriptor = self.archive()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ClientError): materialize(Mock(request=Mock(return_value=data+b'x')), descriptor, directory)


if __name__ == '__main__': unittest.main()
