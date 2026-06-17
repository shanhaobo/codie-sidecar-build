"""Bearer-auth reverse proxy in front of Microsoft's @playwright/mcp.

The MCP streamable-HTTP protocol carries long-lived SSE responses to POSTs;
this proxy has to forward request body in one shot, then stream the
response body chunk-by-chunk as it arrives from the upstream server.
httpx.AsyncClient.stream() + StreamingResponse handles this.

Hop-by-hop response headers that conflict with ASGI framing are stripped
(Content-Length re-derived by Starlette, Transfer-Encoding handled by the
ASGI server, etc).

Per docs/plans/2026-04-24-sidecar-tools-architecture.md Phase 3.
"""

from __future__ import annotations

import os
import sys

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

TOKEN = os.environ.get("BROWSER_MCP_TOKEN", "").strip()
INTERNAL_PORT = int(os.environ.get("BROWSER_MCP_INTERNAL_PORT", "8788"))
# @playwright/mcp ≥0.0.75 rejects requests whose Host header isn't the literal
# string `localhost:<port>` (DNS-rebinding guard). httpx derives Host from the
# URL — using 127.0.0.1 here yields `127.0.0.1:8788` and trips the upstream
# 403 ("Access is only allowed at localhost:8788"). Keep this `localhost`.
INTERNAL_URL = f"http://localhost:{INTERNAL_PORT}"
HOST = os.environ.get("BROWSER_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("BROWSER_MCP_PORT", "8787"))

if not TOKEN:
    print("browser_mcp proxy: BROWSER_MCP_TOKEN not set", file=sys.stderr)
    sys.exit(2)

# Long timeout — SSE responses can run for minutes while browser tasks
# execute upstream. The upstream server owns its own per-call timeouts.
_client = httpx.AsyncClient(timeout=None)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-encoding",
    "content-length",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
}


async def _reject(status_code: int, message: str) -> Response:
    return Response(
        content=f'{{"error":"{message}"}}'.encode(),
        status_code=status_code,
        media_type="application/json",
    )


async def proxy(request: Request) -> Response:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return await _reject(401, "missing bearer")
    if auth[len("Bearer ") :].strip() != TOKEN:
        return await _reject(401, "bearer rejected")

    upstream_url = f"{INTERNAL_URL}{request.url.path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    # Strip Authorization + Host; forward everything else. The upstream
    # server trusts 127.0.0.1 implicitly so no onward auth is needed.
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("authorization", "host")
    }

    body = await request.body()

    upstream_req = _client.build_request(
        request.method, upstream_url, headers=fwd_headers, content=body
    )
    upstream_resp = await _client.send(upstream_req, stream=True)

    async def body_iter():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()

    out_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    return StreamingResponse(
        body_iter(),
        status_code=upstream_resp.status_code,
        headers=out_headers,
    )


app = Starlette(
    routes=[
        Route(
            "/{path:path}",
            proxy,
            methods=[
                "GET",
                "POST",
                "PUT",
                "DELETE",
                "PATCH",
                "OPTIONS",
                "HEAD",
            ],
        ),
    ]
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
