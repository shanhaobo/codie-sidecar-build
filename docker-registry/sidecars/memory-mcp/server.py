"""Codie memory_mcp sidecar — cross-instance long-term memory over MCP.

Exposes four tools (names/signatures are CONTRACT with the agent
behavior guide — do not deviate):

- `memory_store(content, tags?, source?)` — persist one memory.
- `memory_search(query, tags?, limit?)` — FTS5 full-text search.
- `memory_list(n?, tag?)` — newest first, optional exact-tag filter.
- `memory_delete(id)` — remove one memory.

Storage is a single sqlite file (env MEMORY_DB, default /data/memory.db —
the Bridge mounts named volume `codie-memory-data` at /data). The FTS5
index uses the trigram tokenizer (SQLite ≥ 3.34) so CJK text gets
substring matching without word segmentation.

Search query handling: each whitespace token is wrapped in double quotes
before MATCH — this neutralizes FTS5 operators (AND/OR/NOT/*) and gives
plain substring semantics under trigram. Tokens shorter than 3 chars
produce no trigrams and would AND-kill the whole match, so they are
dropped from the FTS query; if NO token reaches 3 chars (e.g. 2-char
Chinese terms like '美式' or '北京 天气'), the search falls back to a
per-token LIKE scan on memories.content — each whitespace token becomes
its own parameterized `content LIKE '%token%'` condition, ANDed together
(rank 0.0).

Bridge's BuiltinMcpSupervisor runs this image as a singleton container
shared by ALL agent instances — cross-instance shared memory is the
point. It passes a per-start bearer token via the MCP_TOKEN env; agents
reach it as MCP server `codie_memory` at http://<host>:8787/mcp
(streamable-HTTP). Auth is a single static bearer, not rotated — the
sidecar is reachable only on the Docker network by paired agents.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_CONTENT_CHARS = 16_000
MIN_SEARCH_LIMIT, MAX_SEARCH_LIMIT = 1, 50
MIN_LIST_N, MAX_LIST_N = 1, 100
# When a tag post-filter is active, over-fetch FTS rows so filtering
# still has enough candidates to fill `limit`.
TAG_FILTER_FETCH = 200

DEFAULT_DB = "/data/memory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  agent_instance TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  embedding BLOB
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content, tags, content='memories', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
  INSERT INTO memories_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;
"""

_conn: sqlite3.Connection | None = None
# FastMCP runs sync tools in a worker-thread pool; one connection
# (check_same_thread=False) guarded by a lock keeps things simple for
# this low-traffic singleton.
_lock = threading.Lock()


def init(db_path: str) -> None:
    """(Re)open the DB at db_path and ensure the schema. Idempotent."""
    global _conn
    close()
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    _conn = conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _db() -> sqlite3.Connection:
    if _conn is None:
        init(os.environ.get("MEMORY_DB", DEFAULT_DB))
    assert _conn is not None
    return _conn


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
    "codie-memory-mcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


def _normalize_tags(tags: list[str]) -> list[str]:
    """Lowercase + strip; commas (the storage separator) become spaces."""
    out: list[str] = []
    for tag in tags:
        t = " ".join(tag.replace(",", " ").lower().split())
        if t and t not in out:
            out.append(t)
    return out


def _split_tags(stored: str) -> list[str]:
    return [t for t in stored.split(",") if t]


def _fts_match(query: str) -> str:
    """Build a trigram-safe MATCH expression: quote every token, drop
    tokens too short to produce a trigram (they'd AND-kill the match)."""
    tokens = [t for t in query.split() if len(t) >= 3]
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _like_pattern(query: str) -> str:
    esc = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _rollback(conn: sqlite3.Connection) -> None:
    """Best-effort rollback after a failed write.

    Legacy isolation opens an implicit transaction on INSERT/DELETE; if
    commit() then fails (disk full etc.) the write stays pending on the
    shared connection and the NEXT successful call's commit would silently
    persist it. Reads (SELECT) never open implicit transactions in legacy
    mode, so memory_search/memory_list need no such handling.
    """
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def _result(row: tuple, rank: float) -> dict:
    mem_id, content, tags, source, created_at = row
    return {
        "id": mem_id,
        "content": content,
        "tags": _split_tags(tags),
        "source": source,
        "created_at": created_at,
        "rank": rank,
    }


@mcp.tool()
def memory_store(content: str, tags: list[str] = [], source: str = "") -> dict:
    """Store a long-term memory shared across all agent instances. Returns {id}."""
    if not content or not content.strip():
        return {"error": "content is empty"}
    if len(content) > MAX_CONTENT_CHARS:
        return {"error": f"content exceeds {MAX_CONTENT_CHARS} chars"}
    tag_str = ",".join(_normalize_tags(tags))
    agent_instance = os.environ.get("CODIE_INSTANCE", "")
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with _lock:
            conn = _db()
            try:
                cur = conn.execute(
                    "INSERT INTO memories (content, tags, source, agent_instance,"
                    " created_at) VALUES (?, ?, ?, ?, ?)",
                    (content, tag_str, source, agent_instance, created_at),
                )
                conn.commit()
            except sqlite3.Error:
                _rollback(conn)
                raise
    except sqlite3.Error as e:
        return {"error": f"memory_store: {e}"}
    return {"id": cur.lastrowid}


@mcp.tool()
def memory_search(query: str, tags: list[str] = [], limit: int = 10) -> dict:
    """Full-text search over stored memories (CJK substring match supported).

    Returns {results: [{id, content, tags, source, created_at, rank}]},
    best match first. Optional tags filter keeps only memories sharing at
    least one of the given tags.
    """
    q = query.strip()
    if not q:
        return {"error": "query is empty"}
    limit = max(MIN_SEARCH_LIMIT, min(int(limit), MAX_SEARCH_LIMIT))
    want = set(_normalize_tags(tags))
    fetch = TAG_FILTER_FETCH if want else limit
    match = _fts_match(q)
    try:
        with _lock:
            if match:
                rows = _db().execute(
                    "SELECT m.id, m.content, m.tags, m.source, m.created_at,"
                    " f.rank FROM memories_fts f"
                    " JOIN memories m ON m.id = f.rowid"
                    " WHERE memories_fts MATCH ? ORDER BY f.rank LIMIT ?",
                    (match, fetch),
                ).fetchall()
            else:
                # Whole query too short for trigram (e.g. 2-char CJK terms):
                # per-token substring scan (AND across whitespace tokens),
                # newest first, rank 0.0.
                tokens = q.split()
                where = " AND ".join(
                    "content LIKE ? ESCAPE '\\'" for _ in tokens
                )
                rows = [
                    (*r, 0.0)
                    for r in _db().execute(
                        "SELECT id, content, tags, source, created_at"
                        f" FROM memories WHERE {where}"
                        " ORDER BY id DESC LIMIT ?",
                        (*(_like_pattern(t) for t in tokens), fetch),
                    )
                ]
    except sqlite3.Error as e:
        return {"error": f"memory_search: {e}"}
    results = []
    for row in rows:
        item = _result(row[:5], float(row[5]))
        if want and not (want & set(item["tags"])):
            continue
        results.append(item)
        if len(results) >= limit:
            break
    return {"results": results}


@mcp.tool()
def memory_list(n: int = 20, tag: str | None = None) -> dict:
    """List stored memories, newest first. Optional exact-tag filter."""
    n = max(MIN_LIST_N, min(int(n), MAX_LIST_N))
    sql = (
        "SELECT id, content, tags, source, created_at FROM memories"
    )
    params: tuple = ()
    normalized = _normalize_tags([tag]) if tag is not None else []
    if normalized:
        sql += " WHERE instr(',' || tags || ',', ',' || ? || ',') > 0"
        params = (normalized[0],)
    sql += " ORDER BY id DESC LIMIT ?"
    params += (n,)
    try:
        with _lock:
            rows = _db().execute(sql, params).fetchall()
    except sqlite3.Error as e:
        return {"error": f"memory_list: {e}"}
    return {"results": [_result(r, 0.0) for r in rows]}


@mcp.tool()
def memory_delete(id: int) -> dict:
    """Delete a memory by id. Returns {deleted: bool} (false when unknown)."""
    try:
        with _lock:
            conn = _db()
            try:
                cur = conn.execute(
                    "DELETE FROM memories WHERE id = ?", (int(id),)
                )
                conn.commit()
            except sqlite3.Error:
                _rollback(conn)
                raise
    except sqlite3.Error as e:
        return {"error": f"memory_delete: {e}"}
    return {"deleted": cur.rowcount > 0}


def main() -> int:
    token = os.environ.get("MCP_TOKEN", "").strip()
    if not token:
        print("memory_mcp: MCP_TOKEN not set; refusing to start", file=sys.stderr)
        return 2
    host = os.environ.get("MEMORY_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MEMORY_MCP_PORT", "8787"))

    init(os.environ.get("MEMORY_DB", DEFAULT_DB))

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
