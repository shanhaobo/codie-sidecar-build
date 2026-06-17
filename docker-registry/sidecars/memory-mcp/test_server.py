"""Tests for the codie-memory-mcp sidecar.

The DB is a per-test tmp_path sqlite file wired through server.init() —
no /data, no docker. Tools are exercised as plain functions (FastMCP's
@tool decorator returns the original function), so no MCP protocol
machinery is spun up.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import server


@pytest.fixture(autouse=True)
def db(tmp_path):
    path = tmp_path / "memory.db"
    server.init(str(path))
    yield path
    server.close()


# --------------------------------------------------------------- memory_store


def test_store_returns_id_and_roundtrips_via_search():
    out = server.memory_store("the quick brown fox", tags=["animals"])
    assert set(out) == {"id"}
    assert isinstance(out["id"], int)

    hits = server.memory_search("quick brown")
    assert [r["id"] for r in hits["results"]] == [out["id"]]
    r = hits["results"][0]
    assert r["content"] == "the quick brown fox"
    assert r["tags"] == ["animals"]
    assert r["source"] == ""
    assert "T" in r["created_at"]  # ISO8601
    assert isinstance(r["rank"], float)


def test_store_normalizes_tags_lowercase_stripped():
    out = server.memory_store("tagged memory", tags=["  Coffee ", "FOO", ""])
    listed = server.memory_list(tag="coffee")
    assert [r["id"] for r in listed["results"]] == [out["id"]]
    assert listed["results"][0]["tags"] == ["coffee", "foo"]


def test_store_empty_content_rejected():
    assert set(server.memory_store("")) == {"error"}
    assert set(server.memory_store("   \n\t ")) == {"error"}


def test_store_content_over_cap_rejected():
    out = server.memory_store("x" * (server.MAX_CONTENT_CHARS + 1))
    assert set(out) == {"error"}
    # At the cap is still fine.
    assert set(server.memory_store("x" * server.MAX_CONTENT_CHARS)) == {"id"}


def test_store_records_agent_instance_from_env(monkeypatch, db):
    monkeypatch.setenv("CODIE_INSTANCE", "inst-uuid-42")
    out = server.memory_store("instance-labelled memory")
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT agent_instance FROM memories WHERE id = ?", (out["id"],)
    ).fetchone()
    conn.close()
    assert row == ("inst-uuid-42",)


def test_store_agent_instance_defaults_empty(monkeypatch, db):
    monkeypatch.delenv("CODIE_INSTANCE", raising=False)
    out = server.memory_store("anonymous memory")
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT agent_instance FROM memories WHERE id = ?", (out["id"],)
    ).fetchone()
    conn.close()
    assert row == ("",)


# -------------------------------------------------------------- memory_search


def test_cjk_trigram_search():
    # Regression: trigram tokenizer gives CJK substring match — '美式咖啡'
    # (4 chars ≥ 3) goes through FTS5 MATCH, no word boundaries needed.
    out = server.memory_store("用户喜欢喝美式咖啡")
    hits = server.memory_search("美式咖啡")
    assert [r["id"] for r in hits["results"]] == [out["id"]]


def test_two_char_cjk_query_like_fallback():
    # '美式' is 2 chars — trigram can't index it; must hit via LIKE fallback.
    out = server.memory_store("用户喜欢喝美式咖啡")
    hits = server.memory_search("美式")
    assert [r["id"] for r in hits["results"]] == [out["id"]]


def test_multi_short_token_query_like_fallback_matches_per_token():
    # '北京 天气' — every token < 3 chars, so the whole query takes the LIKE
    # fallback. Content has both words separately (no literal "北京 天气"
    # substring); per-token AND LIKE must still hit.
    out = server.memory_store("北京今天天气晴")
    hits = server.memory_search("北京 天气")
    assert [r["id"] for r in hits["results"]] == [out["id"]]


def test_mixed_short_and_long_tokens_still_match():
    # A sub-3-char token would AND-kill the whole trigram MATCH if quoted
    # verbatim; short tokens are dropped from the FTS query instead.
    out = server.memory_store("用户喜欢喝美式咖啡")
    hits = server.memory_search("喝 美式咖啡")
    assert [r["id"] for r in hits["results"]] == [out["id"]]


def test_fts_operator_queries_do_not_crash():
    server.memory_store("用户喜欢喝美式咖啡")
    for q in ["AND OR *", "美式 OR 拿铁", 'quote"in"side', "NOT (x)"]:
        out = server.memory_search(q)
        assert "error" not in out, f"query {q!r} broke: {out}"
        assert "results" in out


def test_search_empty_query_rejected():
    assert set(server.memory_search("")) == {"error"}
    assert set(server.memory_search("   ")) == {"error"}


def test_search_tag_filter_intersects():
    a = server.memory_store("coffee preference: americano", tags=["drink", "user"])
    server.memory_store("coffee machine is broken", tags=["hardware"])
    hits = server.memory_search("coffee", tags=["drink"])
    assert [r["id"] for r in hits["results"]] == [a["id"]]


def test_search_limit_clamped():
    for i in range(5):
        server.memory_store(f"common topic memory number {i}")
    assert len(server.memory_search("common topic", limit=0)["results"]) == 1
    assert len(server.memory_search("common topic", limit=3)["results"]) == 3
    # Upper clamp: limit=999 must not blow up; 5 rows exist.
    assert len(server.memory_search("common topic", limit=999)["results"]) == 5


def test_search_no_match_returns_empty_results():
    server.memory_store("something entirely different")
    assert server.memory_search("zzzqqq")["results"] == []


# ---------------------------------------------------------------- memory_list


def test_list_newest_first():
    ids = [server.memory_store(f"memory {i}")["id"] for i in range(3)]
    out = server.memory_list()
    assert [r["id"] for r in out["results"]] == list(reversed(ids))


def test_list_n_clamped():
    for i in range(5):
        server.memory_store(f"memory {i}")
    assert len(server.memory_list(n=0)["results"]) == 1
    assert len(server.memory_list(n=2)["results"]) == 2
    assert len(server.memory_list(n=999)["results"]) == 5


def test_list_tag_filter_exact():
    a = server.memory_store("first", tags=["alpha"])
    server.memory_store("second", tags=["alphabet"])  # not an exact match
    server.memory_store("third", tags=["beta"])
    out = server.memory_list(tag="alpha")
    assert [r["id"] for r in out["results"]] == [a["id"]]


# -------------------------------------------------------------- memory_delete


def test_delete_existing_and_unknown():
    out = server.memory_store("to be deleted")
    assert server.memory_delete(out["id"]) == {"deleted": True}
    assert server.memory_delete(out["id"]) == {"deleted": False}
    assert server.memory_delete(999_999) == {"deleted": False}
    # FTS index updated too: no ghost hits.
    assert server.memory_search("deleted")["results"] == []


# ------------------------------------------------------------- error contract


class FlakyCommitConn:
    """Delegates everything to a real connection but fails commit().

    Simulates disk-full at commit time: the write statement executes (an
    implicit transaction opens on the shared connection), then commit
    raises. Without an explicit rollback the pending write would be
    silently committed by the NEXT successful call's commit.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        raise sqlite3.OperationalError("disk full")


def test_failed_store_commit_rolls_back_pending_insert(monkeypatch):
    real = server._db()
    monkeypatch.setattr(server, "_conn", FlakyCommitConn(real))
    out = server.memory_store("must not survive")
    assert set(out) == {"error"}

    # Connection recovers; the failed insert must NOT resurrect via the
    # next successful call's commit.
    monkeypatch.setattr(server, "_conn", real)
    ok = server.memory_store("second memory")
    assert set(ok) == {"id"}
    listed = server.memory_list()
    assert [r["content"] for r in listed["results"]] == ["second memory"]


def test_failed_delete_commit_rolls_back_pending_delete(monkeypatch):
    kept = server.memory_store("keep me")
    real = server._db()
    monkeypatch.setattr(server, "_conn", FlakyCommitConn(real))
    out = server.memory_delete(kept["id"])
    assert set(out) == {"error"}

    monkeypatch.setattr(server, "_conn", real)
    # A successful unrelated write commits; the failed delete must not
    # ride along with it.
    server.memory_store("unrelated")
    contents = [r["content"] for r in server.memory_list()["results"]]
    assert "keep me" in contents


def test_sqlite_error_surfaces_as_error_dict(monkeypatch):
    class BoomConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        def commit(self):  # pragma: no cover - never reached
            pass

        def rollback(self):
            # Rollback failure must be swallowed; the original error
            # still surfaces as the tool's {"error": ...}.
            raise sqlite3.OperationalError("rollback also failed")

    monkeypatch.setattr(server, "_conn", BoomConn())
    assert set(server.memory_store("x")) == {"error"}
    assert set(server.memory_search("xyz")) == {"error"}
    assert set(server.memory_list()) == {"error"}
    assert set(server.memory_delete(1)) == {"error"}


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
