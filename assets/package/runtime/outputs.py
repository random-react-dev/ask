"""Collect only explicitly returned task files, using descriptor-relative reads."""
import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import secrets
import stat
import subprocess
import time
import unicodedata
import urllib.error
from common import APIRequestError, atomic_json

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILES = 32
MAX_ENTRIES = 4096
MAX_LARGE_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_LARGE_TOTAL_BYTES = MAX_LARGE_FILE_BYTES
RUNTIME_VERSION = '2026.10.20.2'
CAPABILITIES = ['task-output-upload-v1', 'safe-progress-v1']


class OutputError(RuntimeError):
    pass


def _component(name):
    if (not name or name.startswith('.') or name.strip() != name or len(name) > 200
            or any(char in name for char in ('/', '\\', ':'))
            or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in name)):
        raise OutputError('Output collection rejected an unsafe or hidden filename; local files preserved')


def _directory_flags():
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_root(run_dir):
    try:
        parent = os.open(run_dir, _directory_flags())
        try:
            try:
                os.mkdir('outputs', 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            return os.open('outputs', _directory_flags(), dir_fd=parent)
        finally:
            os.close(parent)
    except OSError:
        raise OutputError('Output folder must be a real directory inside this task; local files preserved') from None


def prepare_output_directory(run_dir):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = _open_root(run_dir)
    os.close(root)
    return run_dir / 'outputs'


def _write_response(root, text):
    if not isinstance(text, str):
        raise OutputError('Final assistant response is invalid; local files preserved')
    data = text.encode('utf-8')
    if len(data) > MAX_FILE_BYTES:
        raise OutputError('Final assistant response exceeds the output file size limit; local files preserved')
    temporary = '.runtime-response-' + secrets.token_hex(12)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        for index in range(1, MAX_FILES + 2):
            name = 'response.md' if index == 1 else 'response-' + str(index) + '.md'
            try:
                # link() creates a new directory entry or fails; it cannot
                # replace a requested deliverable with the generated answer.
                os.link(temporary, name, src_dir_fd=root, dst_dir_fd=root, follow_symlinks=False)
                return name
            except FileExistsError:
                current = os.stat(name, dir_fd=root, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode):
                    raise OutputError('Output collection rejected a nonregular response filename; local files preserved')
        raise OutputError('No unused response filename is available; local files preserved')
    finally:
        try:
            os.unlink(temporary, dir_fd=root)
        except FileNotFoundError:
            pass


def _snapshot(root, allow_large=False):
    files, totals = [], {'small_bytes': 0, 'large_bytes': 0, 'entries': 0}
    def walk(directory, parts):
        for name in sorted(os.listdir(directory)):
            totals['entries'] += 1
            if totals['entries'] > MAX_ENTRIES:
                raise OutputError('Output folder contains too many entries; local files preserved')
            _component(name)
            relative_parts = parts + [name]
            relative = '/'.join(relative_parts)
            if len(relative_parts) > 16 or len(relative) > 1024:
                raise OutputError('Output path exceeds the supported length or depth; local files preserved')
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise OutputError('Output collection rejected a symbolic link; local files preserved')
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, _directory_flags(), dir_fd=directory)
                try:
                    opened = os.fstat(child)
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise OutputError('Output directory changed during collection; local files preserved')
                    walk(child, relative_parts)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise OutputError('Output collection accepts only regular task files without extra hard links; local files preserved')
            large = allow_large and before.st_size > MAX_FILE_BYTES
            file_limit = MAX_LARGE_FILE_BYTES if large else MAX_FILE_BYTES
            total_key = 'large_bytes' if large else 'small_bytes'
            total_limit = MAX_LARGE_TOTAL_BYTES if large else MAX_TOTAL_BYTES
            if len(files) >= MAX_FILES or before.st_size > file_limit or totals[total_key] + before.st_size > total_limit:
                raise OutputError('Output files exceed the count or size limit; local files preserved')
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise OutputError('Output file changed during collection; local files preserved')
                chunks, count, digest = [], 0, hashlib.sha256()
                while True:
                    block = os.read(fd, min(65536, file_limit + 1 - count))
                    if not block:
                        break
                    count += len(block)
                    if count > file_limit:
                        raise OutputError('Output file grew beyond its size limit; local files preserved')
                    digest.update(block)
                    if not large:
                        chunks.append(block)
                after = os.fstat(fd)
                if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or count != opened.st_size:
                    raise OutputError('Output file changed while being read; local files preserved')
            finally:
                os.close(fd)
            data = None if large else b''.join(chunks)
            totals[total_key] += count
            if totals[total_key] > total_limit:
                raise OutputError('Output files exceed the total size limit; local files preserved')
            files.append({'path': relative, 'content_type': mimetypes.guess_type(name, strict=False)[0] or 'application/octet-stream', 'size': count, 'sha256': digest.hexdigest(), 'data': data, 'background': large})
    walk(root, [])
    return files


def collect_outputs(run_dir, final_text, allow_large=False):
    root = _open_root(run_dir)
    try:
        response_name = _write_response(root, final_text)
        files = _snapshot(root, allow_large=allow_large)
        for file in files:
            file['generated_response'] = file['path'] == response_name
        return files
    except OutputError:
        raise
    except OSError:
        raise OutputError('Output files could not be safely read; local files preserved') from None
    finally:
        os.close(root)


def _queue_background(api, task, run_dir, file):
    config = getattr(api, 'config', {})
    if not config.get('state_dir') or not config.get('device_id'):
        raise OutputError('Background upload helper configuration is missing; local files preserved')
    state = Path(config['state_dir'])
    identity = json.dumps([config['device_id'], task['id'], task['attempt_id'], file['path'], file['size'], file['sha256']], ensure_ascii=False, separators=(',', ':'))
    upload_id = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]
    command = [str(state / 'bin/device-helper'), 'upload', '--config', str(state / 'config.json'), str(Path(run_dir) / 'outputs' / file['path']), '--task-id', task['id'], '--expect-sha256', file['sha256'], '--expect-size', str(file['size']), '--id', upload_id]
    try:
        process = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        raise OutputError('Background upload could not be queued; local files preserved') from None
    if process.returncode != 0:
        raise OutputError('Background upload helper rejected the file; local files preserved')
    try:
        response = json.loads(process.stdout)
    except ValueError:
        raise OutputError('Background upload helper acknowledgement was invalid; local files preserved') from None
    identifier = response.get('id', response.get('upload_id')) if isinstance(response, dict) else None
    if (identifier != upload_id or response.get('size') != file['size'] or response.get('sha256') != file['sha256']
            or response.get('state') not in ('queued', 'uploading', 'ready', 'retry_wait', 'retrying')):
        raise OutputError('Background upload helper integrity acknowledgement failed; local files preserved')
    return {'id': identifier, 'path': file['path'], 'size': file['size'], 'sha256': file['sha256'], 'generated_response': file['generated_response'], 'delivery': 'background', 'state': response['state']}


def upload_outputs(api, task, run_dir, final_text, attempts=3):
    files = collect_outputs(run_dir, final_text, allow_large=True)
    lease = {key: task[key] for key in ('attempt_id', 'lease_token')}
    uploaded = []
    def save_manifest():
        atomic_json(Path(run_dir) / 'output-manifest.json', {'files': uploaded, 'total_size': sum(file['size'] for file in uploaded)})
    for file in files:
        if file['background']:
            uploaded.append(_queue_background(api, task, run_dir, file))
            save_manifest()
            continue
        payload = {**lease, 'path': file['path'], 'content_type': file['content_type'], 'content_base64': base64.b64encode(file['data']).decode('ascii')}
        for attempt in range(attempts):
            try:
                response = api.request('/api/device/tasks/' + task['id'] + '/outputs', payload)
                break
            except APIRequestError as exc:
                if exc.status not in (408, 429, 500, 502, 503, 504) or attempt + 1 >= attempts:
                    raise OutputError('Output upload failed with HTTP ' + str(exc.status) + '; task not completed and local files preserved') from None
            except (OSError, urllib.error.URLError):
                if attempt + 1 >= attempts:
                    raise OutputError('Output upload transport failed; task not completed and local files preserved') from None
            time.sleep(0.2 * (attempt + 1))
        if not isinstance(response, dict) or response.get('path') != file['path'] or response.get('size') != file['size'] or response.get('sha256') != file['sha256'] or not isinstance(response.get('id'), str) or not response['id']:
            raise OutputError('Output upload integrity acknowledgement failed; task not completed and local files preserved')
        uploaded.append({**{key: response[key] for key in ('id', 'path', 'size', 'sha256')}, 'generated_response': file['generated_response']})
        save_manifest()
    return uploaded
