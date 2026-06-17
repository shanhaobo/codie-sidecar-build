"""Tests for the codie-search-mcp sidecar.

All HTTP is mocked via respx — no real network. Tools are exercised as
plain async functions (FastMCP's @tool decorator returns the original
function), so no MCP protocol machinery is spun up.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import server

TAVILY_URL = "https://api.tavily.com/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SERPER_URL = "https://google.serper.dev/search"


def _set_provider(monkeypatch, provider: str, key: str = "test-key") -> None:
    monkeypatch.setenv("SEARCH_PROVIDER", provider)
    monkeypatch.setenv("SEARCH_API_KEY", key)


# ---------------------------------------------------------------- web_search


@respx.mock
async def test_tavily_request_shape_and_mapping(monkeypatch):
    _set_provider(monkeypatch, "tavily", "tv-key")
    route = respx.post(TAVILY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "T1",
                        "url": "https://example.com/1",
                        "content": "snippet one",
                    },
                    {
                        "title": "T2",
                        "url": "https://example.com/2",
                        "content": "snippet two",
                    },
                ]
            },
        )
    )

    out = await server.web_search("hello world", count=3)

    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["api_key"] == "tv-key"
    assert body["query"] == "hello world"
    assert body["max_results"] == 3
    assert out["results"] == [
        {"title": "T1", "url": "https://example.com/1", "snippet": "snippet one"},
        {"title": "T2", "url": "https://example.com/2", "snippet": "snippet two"},
    ]


@respx.mock
async def test_brave_request_shape_and_mapping(monkeypatch):
    _set_provider(monkeypatch, "brave", "br-key")
    route = respx.get(BRAVE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "B1",
                            "url": "https://example.org/a",
                            "description": "desc a",
                        }
                    ]
                }
            },
        )
    )

    out = await server.web_search("brave query", count=4, lang="de")

    assert route.called
    req = route.calls.last.request
    assert req.headers["X-Subscription-Token"] == "br-key"
    params = dict(httpx.URL(str(req.url)).params)
    assert params["q"] == "brave query"
    assert params["count"] == "4"
    assert params["search_lang"] == "de"
    assert out["results"] == [
        {"title": "B1", "url": "https://example.org/a", "snippet": "desc a"}
    ]


@respx.mock
async def test_serper_request_shape_and_mapping(monkeypatch):
    _set_provider(monkeypatch, "serper", "sp-key")
    route = respx.post(SERPER_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {
                        "title": "S1",
                        "link": "https://example.net/x",
                        "snippet": "serper snippet",
                    }
                ]
            },
        )
    )

    out = await server.web_search("serper query", count=2)

    assert route.called
    req = route.calls.last.request
    assert req.headers["X-API-KEY"] == "sp-key"
    body = json.loads(req.content)
    assert body["q"] == "serper query"
    assert body["num"] == 2
    assert out["results"] == [
        {"title": "S1", "url": "https://example.net/x", "snippet": "serper snippet"}
    ]


@respx.mock
async def test_count_clamped_low_and_high(monkeypatch):
    _set_provider(monkeypatch, "tavily")
    route = respx.post(TAVILY_URL).mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    await server.web_search("q", count=0)
    assert json.loads(route.calls.last.request.content)["max_results"] == 1

    await server.web_search("q", count=99)
    assert json.loads(route.calls.last.request.content)["max_results"] == 10


async def test_unknown_provider_returns_error(monkeypatch):
    _set_provider(monkeypatch, "duckduckgo")
    out = await server.web_search("q")
    assert "error" in out
    assert "duckduckgo" in out["error"]
    assert "SEARCH_PROVIDER" in out["error"]


async def test_missing_api_key_returns_error(monkeypatch):
    monkeypatch.setenv("SEARCH_PROVIDER", "tavily")
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    out = await server.web_search("q")
    assert out == {"error": "SEARCH_API_KEY not configured"}


@respx.mock
async def test_default_provider_is_tavily(monkeypatch):
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("SEARCH_API_KEY", "k")
    route = respx.post(TAVILY_URL).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    out = await server.web_search("q")
    assert route.called
    assert out["results"] == []


# ----------------------------------------------------------------- web_fetch

ARTICLE_PARA = (
    "Trafilatura extracts the main content of a web page and discards "
    "boilerplate such as navigation menus, sidebars and footers. "
)
ARTICLE_HTML = (
    "<html><head><title>Test Article</title></head><body>"
    "<nav>Home | About | Contact</nav>"
    "<article><h1>Test Article</h1>"
    + "".join(f"<p>{ARTICLE_PARA}Paragraph number {i}.</p>" for i in range(30))
    + "</article>"
    "<footer>Copyright 2026</footer>"
    "</body></html>"
)


@respx.mock
async def test_web_fetch_extracts_markdown():
    respx.get("https://example.com/article").mock(
        return_value=httpx.Response(
            200, text=ARTICLE_HTML, headers={"content-type": "text/html"}
        )
    )

    out = await server.web_fetch("https://example.com/article")

    assert out["url"] == "https://example.com/article"
    assert out["truncated"] is False
    assert "Paragraph number 0" in out["markdown"]
    assert "Paragraph number 29" in out["markdown"]
    # Boilerplate stripped.
    assert "Home | About | Contact" not in out["markdown"]


@respx.mock
async def test_web_fetch_truncates_at_max_chars():
    respx.get("https://example.com/article").mock(
        return_value=httpx.Response(
            200, text=ARTICLE_HTML, headers={"content-type": "text/html"}
        )
    )

    # max_chars below the floor gets clamped up to 1000; the article is
    # well over 1000 chars of extracted text, so truncation kicks in.
    out = await server.web_fetch("https://example.com/article", max_chars=10)

    assert out["truncated"] is True
    assert len(out["markdown"]) == 1000


@respx.mock
async def test_web_fetch_caps_body_at_max_fetch_bytes(monkeypatch):
    # Patch the byte budget down so the mocked body exceeds it.
    monkeypatch.setattr(server, "MAX_FETCH_BYTES", 2048)
    assert len(ARTICLE_HTML.encode()) > 2048

    respx.get("https://example.com/big").mock(
        return_value=httpx.Response(
            200, text=ARTICLE_HTML, headers={"content-type": "text/html"}
        )
    )

    seen: dict = {}
    real_extract = server.trafilatura.extract

    def spy(html, **kwargs):
        seen["html_len"] = len(html)
        return real_extract(html, **kwargs)

    monkeypatch.setattr(server.trafilatura, "extract", spy)

    out = await server.web_fetch("https://example.com/big")

    # Fetch succeeds (no error) but only the capped prefix reaches extraction.
    assert "error" not in out
    assert seen["html_len"] <= 2048
    assert "Paragraph number 29" not in out["markdown"]


# ------------------------------------------------------------- error contract


@respx.mock
async def test_web_search_provider_500_returns_error_dict(monkeypatch):
    _set_provider(monkeypatch, "tavily", "secret-api-key")
    respx.post(TAVILY_URL).mock(return_value=httpx.Response(500, text="boom"))

    out = await server.web_search("q")

    assert set(out) == {"error"}
    assert "500" in out["error"]
    assert "secret-api-key" not in out["error"]


@respx.mock
async def test_web_search_non_json_response_returns_error_dict(monkeypatch):
    _set_provider(monkeypatch, "tavily", "secret-api-key")
    respx.post(TAVILY_URL).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )

    out = await server.web_search("q")

    assert set(out) == {"error"}
    assert "secret-api-key" not in out["error"]


@respx.mock
async def test_web_fetch_404_returns_error_dict():
    respx.get("https://example.com/missing").mock(
        return_value=httpx.Response(404, text="nope")
    )

    out = await server.web_fetch("https://example.com/missing")

    assert set(out) == {"error"}
    assert "404" in out["error"]


@respx.mock
async def test_web_fetch_network_error_returns_error_dict():
    respx.get("https://example.com/down").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    out = await server.web_fetch("https://example.com/down")

    assert set(out) == {"error"}


# ------------------------------------------------------------------ app shape


def test_streamable_http_app_serves_mcp_at_root():
    # Bridge's BuiltinMcpSupervisor hardcodes http://<host>:<port>/mcp as the
    # endpoint it hands to agents; pin that the real app (not a synthetic
    # stand-in) exposes a route at exactly /mcp.
    app = server.mcp.streamable_http_app()
    assert any(getattr(r, "path", None) == "/mcp" for r in app.routes)


# --------------------------------------------------------------- bearer auth


def _auth_client(token: str = "secret-token") -> httpx.AsyncClient:
    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok)])
    app.add_middleware(server.BearerAuthMiddleware, token=token)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def test_auth_missing_token_rejected():
    async with _auth_client() as client:
        resp = await client.get("/mcp")
    assert resp.status_code == 401


async def test_auth_wrong_token_rejected():
    async with _auth_client() as client:
        resp = await client.get("/mcp", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


async def test_auth_correct_token_accepted():
    async with _auth_client() as client:
        resp = await client.get(
            "/mcp", headers={"Authorization": "Bearer secret-token"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
