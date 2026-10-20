"""Device runtime primitives. No third party dependencies or personal CLI auth."""
import contextlib
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(value, file, indent=2)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def read_config(path):
    config = json.loads(Path(path).read_text())
    url = urllib.parse.urlsplit(config['server_url'])
    if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise ValueError('server_url requires HTTPS (loopback HTTP only for tests)')
    if url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('server_url must be a clean origin')
    config['server_url'] = config['server_url'].rstrip('/')
    config['state_dir'] = str(Path(config['state_dir']).expanduser().resolve())
    return config


class API:
    def __init__(self, config):
        self.config = config

    def request(self, path, body=None, authenticated=True):
        headers = {'Accept': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.config['device_token']
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.config['server_url'] + path, data=data, headers=headers)
        # No redirect can move a credential to a different origin.
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise APIRequestError(exc.code) from None

    def download(self, path, maximum=5 * 1024 * 1024):
        req = urllib.request.Request(self.config['server_url'] + path, headers={'Authorization': 'Bearer ' + self.config['device_token']})
        opener = urllib.request.build_opener(NoRedirect())
        try:
            with opener.open(req, timeout=30) as response:
                data = response.read(maximum + 1)
                if len(data) > maximum:
                    raise RuntimeError('attachment exceeds the permitted size')
                return data
        except urllib.error.HTTPError as exc:
            raise RuntimeError('attachment request failed: HTTP ' + str(exc.code)) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class APIRequestError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__('server request failed: HTTP ' + str(status))


class ModelRelay:
    """Loopback key only reaches CLI. Server tokens are refreshed per inference request."""
    def __init__(self, api, provider, task=None, diagnostics_path=None):
        self.api, self.provider, self.task = api, provider, task
        self.secret = secrets.token_urlsafe(32)
        self.diagnostics_path = Path(diagnostics_path) if diagnostics_path else None
        self.diagnostic_count = 0
        self.diagnostic_lock = threading.Lock()
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.forward()

            def do_GET(self):
                self.forward()

            def do_DELETE(self):
                self.forward()

            def forward(self):
                started = time.monotonic()
                stage = 'validate-request'
                response_started = False
                supplied = self.headers.get('Authorization', '').removeprefix('Bearer ')
                if not supplied:
                    supplied = self.headers.get('x-api-key', '')
                if not secrets.compare_digest(supplied, relay.secret):
                    self.send_error(401)
                    return
                allowed = '/responses' if relay.provider == 'codex' else '/v1/messages'
                is_mcp = self.path == '/mcp' and relay.task is not None
                request_path = urllib.parse.urlsplit(self.path)
                if request_path.query and not (relay.provider == 'claude' and request_path.query == 'beta=true'):
                    self.send_error(400)
                    return
                if not is_mcp and (self.command != 'POST' or request_path.path != allowed):
                    self.send_error(404)
                    return
                try:
                    stage = 'read-request'
                    length = int(self.headers.get('Content-Length', '0'))
                    if length < 0 or (self.command == 'POST' and length == 0) or length > 16 * 1024 * 1024:
                        self.send_error(413)
                        return
                    body = self.rfile.read(length) if length else None
                    if length and len(body) != length:
                        self.send_error(400)
                        return
                    if relay.task:
                        token_path = '/api/device/tasks/' + relay.task['id'] + '/model-token'
                        token_body = {k: relay.task[k] for k in ('attempt_id', 'lease_token')}
                    else:
                        token_path, token_body = '/api/device/setup/model-token', {'provider': relay.provider}
                    stage = 'exchange-model-token'
                    for attempt in range(2):
                        try:
                            token = relay.api.request(token_path, token_body)['model_token']
                            break
                        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                            if attempt:
                                raise
                            relay.diagnostic('exchange-model-token-retry', self.command, is_mcp, exc, started)
                            time.sleep(0.2)
                    suffix = '/api/research/mcp' if is_mcp else ('/api/model/responses' if relay.provider == 'codex' else '/api/model/anthropic/v1/messages')
                    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
                    headers['X-Device-Provider'] = relay.provider
                    for key in ('anthropic-version', 'anthropic-beta', 'accept', 'mcp-session-id', 'mcp-protocol-version', 'last-event-id', 'mcp-method', 'mcp-name'):
                        if self.headers.get(key):
                            headers[key] = self.headers[key]
                    req = urllib.request.Request(relay.api.config['server_url'] + suffix, body, headers, method=self.command)
                    opener = urllib.request.build_opener(NoRedirect())
                    try:
                        stage = 'connect-upstream'
                        safe_retry = is_mcp and self.command == 'GET'
                        if is_mcp and self.command == 'POST' and body:
                            with contextlib.suppress(ValueError, TypeError):
                                safe_retry = json.loads(body).get('method') in ('initialize', 'tools/list', 'resources/list', 'resources/templates/list', 'prompts/list')
                        for attempt in range(2 if safe_retry else 1):
                            try:
                                upstream = opener.open(req, timeout=180)
                                break
                            except urllib.error.HTTPError:
                                raise
                            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                                if attempt or not safe_retry:
                                    raise
                                relay.diagnostic('connect-upstream-retry', self.command, is_mcp, exc, started)
                                time.sleep(0.2)
                    except urllib.error.HTTPError as exc:
                        relay.diagnostic(stage, self.command, is_mcp, exc, started)
                        self.send_error(exc.code, 'model gateway request failed')
                        exc.close()
                        return
                    with upstream:
                        stage = 'stream-upstream'
                        self.send_response(upstream.status)
                        response_started = True
                        self.send_header('Content-Type', upstream.headers.get('Content-Type', 'application/json'))
                        for header in ('mcp-session-id', 'mcp-protocol-version'):
                            if upstream.headers.get(header):
                                self.send_header(header, upstream.headers[header])
                        self.send_header('Connection', 'close')
                        if upstream.status in (202, 204):
                            self.send_header('Content-Length', '0')
                        self.end_headers()
                        if upstream.status in (202, 204):
                            return
                        while True:
                            block = upstream.read1(65536)
                            if not block:
                                break
                            self.wfile.write(block)
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:
                    relay.diagnostic(stage, self.command, is_mcp, exc, started)
                    if not response_started and not self.wfile.closed:
                        with contextlib.suppress(Exception):
                            self.send_error(502, 'model relay unavailable')
                    else:
                        self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def url(self):
        return 'http://127.0.0.1:' + str(self.server.server_port)

    def diagnostic(self, stage, method, is_mcp, exc, started):
        # Never log URLs, IDs, headers, exception messages, prompts or content.
        with self.diagnostic_lock:
            if self.diagnostic_count >= 32:
                return
            self.diagnostic_count += 1
            reason = getattr(exc, 'reason', None)
            record = {'time': time.time(), 'provider': self.provider, 'route': 'research' if is_mcp else 'model', 'method': method, 'stage': stage, 'exception': type(exc).__name__, 'cause_class': type(reason).__name__ if reason is not None else None, 'errno': getattr(exc, 'errno', getattr(reason, 'errno', None)), 'http_status': getattr(exc, 'status', getattr(exc, 'code', None)), 'elapsed_ms': round((time.monotonic() - started) * 1000)}
            line = json.dumps(record) + '\n'
            if self.diagnostics_path:
                self.diagnostics_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.diagnostics_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, 'a') as file:
                    file.write(line)
            else:
                sys.stderr.write('DEVICE_RELAY_DIAGNOSTIC ' + line)


def preflight_research(relay, timeout=30):
    """Check initialization and required tool definitions without any LLM call."""
    session = None
    protocol = '2025-03-26'
    def exchange(method, params=None, identifier=None):
        nonlocal session
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        if identifier is not None:
            payload['id'] = identifier
        headers = {'Authorization': 'Bearer ' + relay.secret, 'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', 'Mcp-Protocol-Version': protocol}
        if session:
            headers['Mcp-Session-Id'] = session
        request = urllib.request.Request(relay.url + '/mcp', json.dumps(payload).encode(), headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            session = response.headers.get('mcp-session-id', session)
            if identifier is None:
                if response.status not in (200, 202, 204):
                    raise RuntimeError('research notification was rejected')
                return None
            if 'text/event-stream' not in response.headers.get('content-type', ''):
                result = json.loads(response.read(1024 * 1024 + 1))
            else:
                data, total = [], 0
                while True:
                    raw = response.readline(1024 * 1024 + 1)
                    total += len(raw)
                    if total > 1024 * 1024 or not raw:
                        raise RuntimeError('research initialization stream incomplete')
                    line = raw.decode().rstrip('\r\n')
                    if line.startswith('data:'):
                        data.append(line[5:].lstrip())
                    if not line and data:
                        result = json.loads('\n'.join(data))
                        if result.get('id') == identifier:
                            break
                        data = []
            if result.get('id') != identifier or result.get('error') or 'result' not in result:
                raise RuntimeError('research initialization response invalid')
            return result['result']
    try:
        init = exchange('initialize', {'protocolVersion': protocol, 'capabilities': {}, 'clientInfo': {'name': 'device-agent-preflight', 'version': '1.0'}}, 1)
        protocol = init.get('protocolVersion', protocol)
        exchange('notifications/initialized')
        listed = exchange('tools/list', {}, 2)
        names = {tool.get('name') for tool in listed.get('tools', [])}
        if not {'research_search', 'research_fetch'} <= names:
            raise RuntimeError('required research tools were not advertised')
        return {'verified_at': time.time(), 'required_tools': ['research_search', 'research_fetch'], 'protocol_version': protocol}
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError('required research MCP preflight failed; model was not started') from None
    finally:
        if session:
            request = urllib.request.Request(relay.url + '/mcp', headers={'Authorization': 'Bearer ' + relay.secret, 'Mcp-Session-Id': session, 'Mcp-Protocol-Version': protocol}, method='DELETE')
            with contextlib.suppress(Exception):
                urllib.request.urlopen(request, timeout=5).close()


def clean_environment():
    # Explicit allowlist avoids OAuth, subscription, provider or plugin credentials.
    allowed = ('PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'SHELL', 'USER', 'LOGNAME', 'SYSTEMROOT', 'SSL_CERT_FILE', 'SSL_CERT_DIR')
    return {key: value for key, value in os.environ.items() if key in allowed}


def build_cli(config, provider, run_dir, relay):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = clean_environment()
    model = config.get('model', 'deepseek-flash')
    if model not in ('deepseek-flash', 'deepseek-v4-pro'):
        raise ValueError('model is not allowlisted')
    research_url = relay.url + '/mcp' if relay.task and config.get('research_gateway_enabled', True) else None
    research_secret = None
    if research_url:
        research_secret = relay.secret
    if provider == 'codex':
        home = run_dir / 'codex-home'
        home.mkdir(mode=0o700, exist_ok=True)
        env.update(CODEX_HOME=str(home), DEVICE_MODEL_TOKEN=relay.secret)
        catalog = str(ROOT / 'models.json')
        values = {
            'model': model, 'model_provider': 'device', 'approval_policy': 'never',
            'sandbox_mode': 'danger-full-access', 'web_search': 'disabled', 'notify': [],
            'model_reasoning_effort': config.get('reasoning_effort', 'high'),
            'model_catalog_json': catalog,
            'model_providers.device.name': 'Device DeepSeek gateway',
            'model_providers.device.base_url': relay.url,
            'model_providers.device.env_key': 'DEVICE_MODEL_TOKEN',
            'model_providers.device.wire_api': 'responses',
            'model_providers.device.requires_openai_auth': False,
            'model_providers.device.supports_websockets': False,
        }
        if research_url:
            env['DEVICE_RESEARCH_TOKEN'] = research_secret
            values.update({'mcp_servers.research-gateway.url': research_url,
                           'mcp_servers.research-gateway.bearer_token_env_var': 'DEVICE_RESEARCH_TOKEN',
                           'mcp_servers.research-gateway.startup_timeout_sec': 30,
                           'mcp_servers.research-gateway.tool_timeout_sec': 180})
            values['mcp_servers.research-gateway.required'] = True
        args = [config.get('codex_bin', 'codex'), 'exec', '--json', '--ephemeral', '--skip-git-repo-check', '--ignore-user-config', '--sandbox', 'danger-full-access', '-C', str(run_dir), '-o', str(run_dir / 'result.txt')]
        for image_path in config.get('image_paths', []):
            args += ['--image', str(image_path)]
        for feature in ('plugins', 'remote_plugin', 'computer_use', 'browser_use', 'apps', 'hooks'):
            args += ['--disable', feature]
        for key, value in values.items():
            args += ['-c', key + '=' + json.dumps(value)]
        args += ['-']
    elif provider == 'claude':
        home = run_dir / 'claude-home'
        home.mkdir(mode=0o700, exist_ok=True)
        env.update(CLAUDE_CONFIG_DIR=str(home), ANTHROPIC_API_KEY=relay.secret,
                   ANTHROPIC_BASE_URL=relay.url, ANTHROPIC_MODEL=model,
                   ANTHROPIC_DEFAULT_OPUS_MODEL=model, ANTHROPIC_DEFAULT_SONNET_MODEL=model,
                   ANTHROPIC_DEFAULT_HAIKU_MODEL=model, CLAUDE_CODE_SUBAGENT_MODEL=model,
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1',
                   CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL='1')
        mcp = {'mcpServers': {}}
        if research_url:
            mcp['mcpServers']['research-gateway'] = {'type': 'http', 'url': research_url, 'headers': {'Authorization': 'Bearer ' + research_secret}}
        mcp_path = run_dir / 'mcp.json'
        atomic_json(mcp_path, mcp)
        args = [config.get('claude_bin', 'claude'), '-p', '--bare', '--permission-mode', 'bypassPermissions', '--dangerously-skip-permissions', '--permission-prompts', 'none', '--no-chrome', '--no-session-persistence', '--setting-sources', '', '--strict-mcp-config', '--mcp-config', str(mcp_path), '--settings', '{"disableAllHooks":true}', '--tools', 'Bash,Edit,Write,Read,Glob,Grep', '--output-format', 'stream-json', '--verbose', '--model', model]
    else:
        raise ValueError('provider must be codex or claude')
    return args, env


def terminate_group(process):
    import signal
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        process.poll()  # Reap the group leader even if its descendants outlive it.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # macOS can temporarily refuse the probe while an orphaned group
            # is being reaped. Retry the exact owned group within the deadline.
            pass
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        raise RuntimeError('unable to terminate owned CLI process group') from None
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


def run_cli(config, provider, run_dir, relay, prompt, on_event=None, stop_event=None):
    args, env = build_cli(config, provider, run_dir, relay)
    research_required = bool(getattr(relay, 'task', None) and config.get('research_gateway_enabled', True))
    if research_required:
        atomic_json(Path(run_dir) / 'research-preflight.json', preflight_research(relay))
    atomic_json(Path(run_dir) / 'invocation.json', {'argv': args, 'provider': provider, 'model': config.get('model', 'deepseek-flash'), 'auth_source': 'server-model-token-via-loopback', 'started_at': time.time()})
    with open(Path(run_dir) / 'events.jsonl', 'wb') as out, open(Path(run_dir) / 'stderr.log', 'wb') as err:
        process = subprocess.Popen(args, cwd=run_dir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, start_new_session=True)
        process.stdin.write(prompt.encode())
        process.stdin.close()
        semantic_failure = []
        final_events = []
        claude_mcp_ready = []
        def consume():
            for line in process.stdout:
                out.write(line)
                out.flush()
                try:
                    event = json.loads(line)
                    # Codex emits type:error for recoverable connection retries.
                    # A terminal failed turn, missing completion or nonzero exit
                    # is authoritative; a recovered transport warning is not.
                    if event.get('type') == 'turn.failed':
                        semantic_failure.append(event.get('type'))
                    if event.get('type') in ('turn.completed', 'result'):
                        final_events.append(event.get('type'))
                    if provider == 'claude' and research_required and event.get('type') == 'system' and event.get('subtype') == 'init':
                        connected = any(server.get('name') == 'research-gateway' and server.get('status') == 'connected' for server in event.get('mcp_servers', []))
                        claude_mcp_ready.append(connected)
                    if on_event:
                        on_event(event)
                except (ValueError, TypeError):
                    pass
        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        started = time.monotonic()
        try:
            while process.poll() is None:
                if (stop_event and stop_event.is_set()) or time.monotonic() - started > config.get('max_job_seconds', 3600):
                    terminate_group(process)
                    raise RuntimeError('job stopped: lease unavailable, pause, or time limit')
                time.sleep(0.2)
            consumer.join(timeout=5)
            result_file = Path(run_dir) / 'result.txt'
            final = result_file.read_text() if result_file.exists() else ''
            if provider == 'claude':
                last_result = None
                for line in (Path(run_dir) / 'events.jsonl').read_text().splitlines():
                    with contextlib.suppress(ValueError):
                        event = json.loads(line)
                        if event.get('type') == 'result':
                            last_result = event
                if last_result is not None:
                    final = last_result.get('result', '')
                    if last_result.get('is_error'):
                        raise RuntimeError('Claude reported unsuccessful result')
            if process.returncode != 0:
                raise RuntimeError('CLI exited with status ' + str(process.returncode))
            if semantic_failure or not final_events:
                raise RuntimeError('CLI did not report a successful completed turn')
            if provider == 'claude' and research_required and not any(claude_mcp_ready):
                raise RuntimeError('required research MCP was not connected in Claude startup')
            return {'text': final[-100000:], 'exit_code': process.returncode, 'duration_seconds': round(time.monotonic() - started, 3), 'events_sha256': hashlib.sha256((Path(run_dir) / 'events.jsonl').read_bytes()).hexdigest()}
        finally:
            terminate_group(process)
            process.stdout.close()
