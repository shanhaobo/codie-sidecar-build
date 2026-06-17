"""Codie search_mcp sidecar — web search + page fetch over MCP.

Exposes two tools:

- `web_search(query, count?, lang?)` — dispatches to one of three search
  providers (tavily | brave | serper) based on the SEARCH_PROVIDER env,
  authenticated with SEARCH_API_KEY. Results are normalized to
  [{title, url, snippet}].
- `web_fetch(url, max_chars?)` — fetches a page directly and extracts its
  main content as markdown via trafilatura. No search-API quota consumed.

Bridge's BuiltinMcpSupervisor runs this image as a singleton container,
passing a per-start bearer token via the MCP_TOKEN env. Agent containers
reach it as MCP server `codie_search` at http://<host>:8787/mcp
(streamable-HTTP).

Auth is a single static bearer, not rotated — the sidecar is reachable
only on the Docker network by paired agents. If the token is ever
suspected leaked, Bridge just restarts the sidecar.

web_fetch's egress is deliberately unrestricted: the paired agent
containers already have full network egress, so gating URLs here adds no
security boundary (the bearer gate is the boundary); revisit if this
sidecar is ever exposed beyond the Docker network.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
import trafilatura
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

MIN_COUNT, MAX_COUNT = 1, 10
MIN_FETCH_CHARS, MAX_FETCH_CHARS = 1000, 200_000
MAX_FETCH_BYTES = 5 * 1024 * 1024  # raw download byte budget for web_fetch
FETCH_DEADLINE_SECONDS = 60  # wall-clock cap (httpx timeout is per-operation)

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SERPER_URL = "https://google.serper.dev/search"

_client = httpx.AsyncClient(
    follow_redirects=True,
    timeout=30,
    headers={"User-Agent": USER_AGENT},
)


class BearerAuthMiddleware:
    """Static-bearer ASGI gate. Token read once from env at startup."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            resp = JSONResponse({"error": "missing bearer"}, status_code=401)
            await resp(scope, receive, send)
            return
        if auth[len("Bearer ") :].strip() != self._token:
            resp = JSONResponse({"error": "bearer rejected"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


mcp = FastMCP(
    "codie-search-mcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


async def _search_tavily(query: str, count: int, lang: str | None, key: str) -> list:
    # Tavily has no first-class language param — ignore `lang`.
    r = await _client.post(
        TAVILY_URL,
        json={"api_key": key, "query": query, "max_results": count},
    )
    r.raise_for_status()
    return [
        {
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "snippet": it.get("content", ""),
        }
        for it in r.json().get("results", [])
    ]


async def _search_brave(query: str, count: int, lang: str | None, key: str) -> list:
    params: dict = {"q": query, "count": count}
    if lang:
        params["search_lang"] = lang
    r = await _client.get(
        BRAVE_URL,
        params=params,
        headers={"X-Subscription-Token": key},
    )
    r.raise_for_status()
    return [
        {
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "snippet": it.get("description", ""),
        }
        for it in r.json().get("web", {}).get("results", [])
    ]


async def _search_serper(query: str, count: int, lang: str | None, key: str) -> list:
    # Serper has no portable language param — ignore `lang`.
    r = await _client.post(
        SERPER_URL,
        json={"q": query, "num": count},
        headers={"X-API-KEY": key},
    )
    r.raise_for_status()
    return [
        {
            "title": it.get("title", ""),
            "url": it.get("link", ""),
            "snippet": it.get("snippet", ""),
        }
        for it in r.json().get("organic", [])
    ]


_PROVIDERS = {
    "tavily": _search_tavily,
    "brave": _search_brave,
    "serper": _search_serper,
}


def _error_text(e: Exception, *, redact: str = "") -> str:
    """Render an exception for the {"error": ...} contract.

    Includes the HTTP status code when available; never leaks the API key
    (redacted defensively even though no provider puts it in the URL).
    """
    if isinstance(e, httpx.HTTPStatusError):
        msg = f"HTTP {e.response.status_code} from {e.request.url}"
    else:
        msg = f"{type(e).__name__}: {e}"
    if redact:
        msg = msg.replace(redact, "[redacted]")
    return msg


@mcp.tool()
async def web_search(query: str, count: int = 5, lang: str | None = None) -> dict:
    """Search the web. Returns results [{title, url, snippet}]. Use web_fetch to read a result in full."""
    provider = os.environ.get("SEARCH_PROVIDER", "tavily").strip().lower() or "tavily"
    search = _PROVIDERS.get(provider)
    if search is None:
        return {"error": f"unknown SEARCH_PROVIDER {provider}"}
    key = os.environ.get("SEARCH_API_KEY", "").strip()
    if not key:
        return {"error": "SEARCH_API_KEY not configured"}
    count = max(MIN_COUNT, min(int(count), MAX_COUNT))
    try:
        results = await search(query, count, lang, key)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, AttributeError) as e:
        # Provider 4xx/5xx, network errors, malformed JSON, shape surprises —
        # all surface as the same {"error": ...} contract as config errors.
        return {"error": f"web_search ({provider}): {_error_text(e, redact=key)}"}
    return {"provider": provider, "results": results}


@mcp.tool()
async def web_fetch(url: str, max_chars: int = 20000) -> dict:
    """Fetch a web page and return its main content as markdown. Free (no search API quota).

    max_chars is clamped to [1000, 200000]; the raw download itself is
    capped at MAX_FETCH_BYTES (5 MiB) and a 60s wall-clock deadline.
    """
    max_chars = max(MIN_FETCH_CHARS, min(int(max_chars), MAX_FETCH_CHARS))
    try:
        async with asyncio.timeout(FETCH_DEADLINE_SECONDS):
            async with _client.stream(
                "GET", url, headers={"User-Agent": USER_AGENT}
            ) as r:
                r.raise_for_status()
                body = b""
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_FETCH_BYTES:
                        body = body[:MAX_FETCH_BYTES]
                        break
                final_url = str(r.url)
                encoding = r.charset_encoding or "utf-8"
    except TimeoutError:
        return {"error": f"web_fetch: deadline of {FETCH_DEADLINE_SECONDS}s exceeded"}
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, AttributeError) as e:
        return {"error": f"web_fetch: {_error_text(e)}"}
    try:
        html = body.decode(encoding, errors="replace")
    except LookupError:  # server declared a bogus charset
        html = body.decode("utf-8", errors="replace")
    text = (
        trafilatura.extract(
            html, url=url, output_format="markdown", include_links=True
        )
        or ""
    )
    return {
        "url": final_url,
        "markdown": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


def main() -> int:
    token = os.environ.get("MCP_TOKEN", "").strip()
    if not token:
        print("search_mcp: MCP_TOKEN not set; refusing to start", file=sys.stderr)
        return 2
    host = os.environ.get("SEARCH_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("SEARCH_MCP_PORT", "8787"))

    # Bridge's BuiltinMcpSupervisor points agents at http://...:<port>/mcp —
    # FastMCP's streamable_http_app already serves at /mcp, so use it as the
    # root app directly (no nested mount; media-mcp's mount dance would put
    # the endpoint at /mcp/mcp instead).
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, token=token)

    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
