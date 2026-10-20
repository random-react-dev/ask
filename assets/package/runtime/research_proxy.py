"""Authenticated pass-through to the owner's existing fixed research MCP endpoint."""
import inspect
import os
from pathlib import Path
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import httpx

ENDPOINT = 'https://research-gateway.r95426882.workers.dev/mcp'
MAX_BODY = 1024 * 1024


def create_research_router(authorize_model):
    router = APIRouter()
    sessions = {}

    @router.api_route('/api/research/mcp', methods=['GET', 'POST', 'DELETE'])
    async def research(request: Request):
        auth = request.headers.get('authorization', '')
        provider = request.headers.get('x-device-provider', '')
        if not auth.startswith('Bearer ') or provider not in ('codex', 'claude'):
            raise HTTPException(401, 'task model credential required')
        principal = authorize_model(auth[7:], provider)
        if inspect.isawaitable(principal):
            principal = await principal
        if not principal or principal.get('scope') != 'task':
            raise HTTPException(403, 'research is available to active tasks')
        secret = os.environ.get('RESEARCH_GATEWAY_TOKEN', '')
        if not secret and os.environ.get('RESEARCH_GATEWAY_TOKEN_FILE'):
            try:
                secret = Path(os.environ['RESEARCH_GATEWAY_TOKEN_FILE']).read_text().strip()
            except OSError:
                pass
        if not secret:
            raise HTTPException(503, 'research gateway is not configured')
        # Old and current Streamable HTTP clients are both supported. Bind any
        # legacy upstream session to its authenticated task, independent of token rotation.
        owner = tuple(principal.get(k) for k in ('device_id', 'task_id', 'attempt_id', 'provider'))
        now = time.monotonic()
        for sid in list(sessions):
            if sessions[sid][1] < now:
                del sessions[sid]
        sid = request.headers.get('mcp-session-id')
        if sid and (sid not in sessions or sessions[sid][0] != owner):
            raise HTTPException(404, 'research session not found')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, 'research request too large')
        headers = {'Authorization': 'Bearer ' + secret, 'Accept': request.headers.get('accept', 'application/json, text/event-stream'), 'Accept-Encoding': 'identity'}
        for name in ('content-type', 'mcp-session-id', 'mcp-protocol-version', 'last-event-id', 'mcp-method', 'mcp-name'):
            if request.headers.get(name):
                headers[name] = request.headers[name]
        client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), follow_redirects=False)
        try:
            response = await client.send(client.build_request(request.method, ENDPOINT, content=bytes(body) or None, headers=headers), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502, 'research gateway unavailable') from None
        response_headers = {'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'}
        upstream_sid = response.headers.get('mcp-session-id')
        if upstream_sid and response.status_code < 300:
            if len(sessions) > 10000:
                await response.aclose()
                await client.aclose()
                raise HTTPException(503, 'research session capacity reached')
            sessions[upstream_sid] = (owner, now + 3600)
            response_headers['mcp-session-id'] = upstream_sid
        if request.method == 'DELETE' and sid and response.status_code < 300:
            sessions.pop(sid, None)
        if response.status_code >= 400:
            status = response.status_code if response.status_code in (400, 404, 405, 406, 429, 503) else 502
            await response.aclose()
            await client.aclose()
            raise HTTPException(status, 'research gateway rejected request')
        async def stream():
            try:
                async for chunk in response.aiter_bytes():
                    if await request.is_disconnected():
                        break
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()
        return StreamingResponse(stream(), status_code=response.status_code, media_type=response.headers.get('content-type', 'application/json'), headers=response_headers)
    return router
