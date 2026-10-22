"""Small HTTPS client and immutable release cache; no application-specific actions."""
import hashlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

MAX_PACKAGE = 128 * 1024 * 1024


class ClientError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError('Service redirects are not allowed')


class Client:
    """Read-only access to published runner releases."""
    def __init__(self, origin):
        url = urllib.parse.urlsplit(origin)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise ClientError('Service must be a clean HTTPS origin')
        self.origin = origin.rstrip('/')

    def request(self, path, raw=False):
        if not path.startswith('/api/public/runner/'):
            raise ClientError('Only public runner downloads are supported')
        for attempt in range(4):
            delay = random.uniform(0, min(8, 2 ** attempt))
            try:
                request = urllib.request.Request(self.origin + path, headers={'Accept': 'application/json', 'User-Agent': 'ask/1.0'})
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                    content = response.read(MAX_PACKAGE + 1)
                if len(content) > MAX_PACKAGE:
                    raise ClientError('Service response exceeds download limit')
                try:
                    return content if raw else json.loads(content)
                except ValueError:
                    raise ClientError('Service returned invalid JSON') from None
            except urllib.error.HTTPError as exc:
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 3:
                    raise ClientError('Download failed (HTTP %s)' % exc.code) from None
                if exc.headers.get('Retry-After', '').isdigit():
                    delay = min(30, int(exc.headers['Retry-After']))
                exc.close()
            except (OSError, urllib.error.URLError) as exc:
                if attempt == 3:
                    raise ClientError('Connection failed: ' + type(exc).__name__) from None
            time.sleep(delay)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def materialize(client, descriptor, cache):
    digest = descriptor.get('sha256', '')
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ClientError('Invalid artifact digest')
    if not 0 < descriptor.get('size', 0) <= MAX_PACKAGE:
        raise ClientError('Invalid artifact size')
    if descriptor.get('url') != '/api/public/runner/artifacts/' + digest + '.zip':
        raise ClientError('Artifact must come from the configured service')
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / digest
    entry = descriptor.get('entrypoint', '')
    if not entry or entry.startswith('/') or '..' in Path(entry).parts:
        raise ClientError('Invalid artifact entry point')
    if target.is_symlink():
        raise ClientError('Release cache must not be a symlink')
    if (target / '.verified.json').is_file():
        checks = json.loads((target / '.verified.json').read_text())
        if all((target / name).is_file() and not (target / name).is_symlink() and hashlib.sha256((target / name).read_bytes()).hexdigest() == sha for name, sha in checks.items()) and entry in checks:
            return target
        raise ClientError('Cached release changed; refusing to execute it')
    blob = client.request(descriptor['url'], raw=True)
    if len(blob) != descriptor['size'] or hashlib.sha256(blob).hexdigest() != digest:
        raise ClientError('Artifact integrity verification failed')
    staging = Path(tempfile.mkdtemp(dir=cache, prefix='.release-'))
    try:
        checks = {}
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            total = 0
            for item in archive.infolist():
                name = item.filename
                parts = Path(name).parts
                mode = item.external_attr >> 16
                if name.startswith('/') or '\\' in name or '..' in parts or not parts or name in checks or name == '.verified.json' or stat.S_ISLNK(mode):
                    raise ClientError('Unsafe artifact archive')
                if item.is_dir():
                    continue
                total += item.file_size
                if total > MAX_PACKAGE or len(checks) >= 256:
                    raise ClientError('Expanded artifact exceeds limits')
                data = archive.read(item)
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as stream:
                    stream.write(data)
                path.chmod(0o700 if mode & 0o111 else 0o600)
                checks[name] = hashlib.sha256(data).hexdigest()
        if entry not in checks:
            raise ClientError('Artifact entry point is missing')
        atomic_json(staging / '.verified.json', checks)
        try:
            os.rename(staging, target)
        except FileExistsError:
            return materialize(client, descriptor, cache)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)
