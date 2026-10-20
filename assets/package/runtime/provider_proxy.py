"""Fixed-route native DeepSeek proxy. No Chat Completions/Responses conversion."""
import inspect
import json
import os
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import httpx

UPSTREAM = 'https://api.deepseek.com'
MODELS = frozenset(('deepseek-flash', 'deepseek-v4-pro'))
MAX_BODY = 16 * 1024 * 1024


def create_router(authorize_model):
    router = APIRouter()
    budgets = {}

    async def proxy(request, provider, suffix):
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer '):
            raise HTTPException(401, 'model token required')
        principal = authorize_model(authorization[7:], provider)
        if inspect.isawaitable(principal):
            principal = await principal
        if not principal:
            raise HTTPException(401, 'invalid model token')
        key = os.environ.get('DEEPSEEK_API_KEY', '')
        if not key:
            raise HTTPException(503, 'model provider is not configured')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, 'model request too large')
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'invalid model JSON') from None
        if not isinstance(payload, dict) or payload.get('model') not in MODELS:
            raise HTTPException(400, 'model is not allowlisted')
        maximum = int(os.environ.get('MODEL_MAX_OUTPUT_TOKENS', '32768'))
        field = 'max_output_tokens' if provider == 'codex' else 'max_tokens'
        requested = payload.get(field, maximum)
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
            raise HTTPException(400, 'invalid output token limit')
        payload[field] = min(requested, maximum)
        # No stateful Responses references: DeepSeek is explicitly stateless.
        if provider == 'codex':
            if payload.get('previous_response_id'):
                raise HTTPException(400, 'send full input history; provider is stateless')
            payload['store'] = False
        owner = tuple(principal.get(k) for k in ('device_id', 'task_id', 'attempt_id', 'provider', 'scope'))
        now = time.monotonic()
        for identifier in list(budgets):
            if now - budgets[identifier]['last_seen'] > 86400 and not budgets[identifier]['active']:
                del budgets[identifier]
        if owner not in budgets and len(budgets) >= 10000:
            raise HTTPException(503, 'model gateway capacity reached')
        budget = budgets.setdefault(owner, {'requests': 0, 'active': 0, 'last_seen': now})
        if budget['active'] >= int(os.environ.get('MODEL_MAX_CONCURRENT_PER_TASK', '2')):
            raise HTTPException(429, 'task inference concurrency reached')
        if budget['requests'] >= int(os.environ.get('MODEL_MAX_REQUESTS_PER_TASK', '500')):
            raise HTTPException(429, 'task model request budget reached')
        budget.update(requests=budget['requests'] + 1, active=budget['active'] + 1, last_seen=now)
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key, 'Accept-Encoding': 'identity'}
        if provider == 'claude':
            headers['x-api-key'] = key
            headers['anthropic-version'] = request.headers.get('anthropic-version', '2023-06-01')
        client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), follow_redirects=False)
        try:
            upstream = await client.send(client.build_request('POST', UPSTREAM + suffix, json=payload, headers=headers), stream=True)
        except httpx.HTTPError:
            budget['active'] -= 1
            await client.aclose()
            raise HTTPException(502, 'model provider unavailable') from None
        if upstream.status_code != 200:
            budget['active'] -= 1
            status = upstream.status_code if upstream.status_code in (400, 401, 403, 413, 429, 503) else 502
            await upstream.aclose()
            await client.aclose()
            # Never echo provider bodies, request headers, key or task prompts.
            raise HTTPException(status, 'model provider rejected request')
        async def body_stream():
            try:
                # httpx may receive compressed responses even when identity was
                # requested. Decode before forwarding because downstream has no
                # Content-Encoding/Content-Length from that compressed transfer.
                async for chunk in upstream.aiter_bytes():
                    if await request.is_disconnected():
                        break
                    yield chunk
            finally:
                budget['active'] -= 1
                await upstream.aclose()
                await client.aclose()
        return StreamingResponse(body_stream(), status_code=200, media_type=upstream.headers.get('content-type', 'application/json'), headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    @router.post('/api/model/responses')
    async def responses(request: Request):
        return await proxy(request, 'codex', '/responses')

    @router.post('/api/model/anthropic/v1/messages')
    async def messages(request: Request):
        return await proxy(request, 'claude', '/anthropic/v1/messages')

    return router
