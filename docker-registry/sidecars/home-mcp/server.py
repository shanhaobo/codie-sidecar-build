"""Codie home_mcp sidecar — Home Assistant REST proxy over MCP.

Exposes three tools against the Home Assistant REST API at HA_BASE_URL,
authenticated with the HA_TOKEN long-lived access token:

- `home_list_entities(domain?)` — GET /api/states, optionally filtered by
  domain. Attributes are stripped down to friendly_name only (full
  attribute dumps blow up agent context) and the list is capped at
  MAX_ENTITIES with a `truncated` flag.
- `home_get_state(entity_id)` — GET /api/states/{entity_id}; full
  attributes (a single entity is fine).
- `home_call_service(domain, service, entity_id, data?)` — POST
  /api/services/{domain}/{service} with {"entity_id": ..., **data}.

The proxy is deliberately mechanical: safety confirmation for locks /
alarm panels is the AGENT's job (behavior guide), not enforced here.

Bridge's BuiltinMcpSupervisor runs this image as a singleton container,
passing a per-start bearer token via the MCP_TOKEN env. Agent containers
reach it as MCP server `codie_home` at http://<host>:8787/mcp
(streamable-HTTP).

Auth is a single static bearer, not rotated — the sidecar is reachable
only on the Docker network by paired agents. If the token is ever
suspected leaked, Bridge just restarts the sidecar.
"""

from __future__ import annotations

import os
import re
import sys

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_ENTITIES = 500  # home_list_entities cap; sets `truncated` when exceeded

# Shape gates applied BEFORE any value is interpolated into the HA URL —
# rejects path injection like "light/../hack". Checked with fullmatch (not
# match) so a trailing "\n" can't sneak past `$`.
_DOMAIN_RE = re.compile(r"[a-z_]+")
_SERVICE_RE = re.compile(r"[a-z_]+")
_ENTITY_ID_RE = re.compile(r"[a-z_]+\.[A-Za-z0-9_]+")

_client = httpx.AsyncClient(timeout=15)


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
    "codie-home-mcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


def _ha_config() -> tuple[str, str] | None:
    """Read HA endpoint config lazily (per call) so env changes need no reload."""
    base = os.environ.get("HA_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("HA_TOKEN", "").strip()
    if not base or not token:
        return None
    return base, token


_CONFIG_ERROR = {"error": "HA_BASE_URL/HA_TOKEN not configured"}


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _error_text(e: Exception, *, base: str, token: str) -> str:
    """Render an exception for the {"error": ...} contract.

    Includes the HTTP status code when available; an unreachable HA gets a
    friendly message naming HA_BASE_URL. HA_TOKEN is never echoed —
    redacted defensively even though it only travels in a header.
    """
    if isinstance(e, httpx.HTTPStatusError):
        msg = f"HTTP {e.response.status_code} from {e.request.url}"
    elif isinstance(e, httpx.ConnectError):
        msg = f"Home Assistant at {base} is unreachable ({e})"
    else:
        msg = f"{type(e).__name__}: {e}"
    if token:
        msg = msg.replace(token, "[redacted]")
    return msg


# Provider 4xx/5xx, network errors, malformed JSON, shape surprises (e.g. a
# bare JSON scalar where a list/dict is expected → TypeError) — all surface
# as the same {"error": ...} contract as config errors.
_TOOL_ERRORS = (
    httpx.HTTPError,
    httpx.InvalidURL,
    ValueError,
    AttributeError,
    TypeError,
)


@mcp.tool()
async def home_list_entities(domain: str | None = None) -> dict:
    """List Home Assistant entities as [{entity_id, state, friendly_name}], optionally filtered by domain (e.g. "light")."""
    cfg = _ha_config()
    if cfg is None:
        return dict(_CONFIG_ERROR)
    base, token = cfg
    try:
        r = await _client.get(f"{base}/api/states", headers=_auth_headers(token))
        r.raise_for_status()
        entities = [
            {
                "entity_id": it.get("entity_id", ""),
                "state": it.get("state", ""),
                "friendly_name": (it.get("attributes") or {}).get(
                    "friendly_name", ""
                ),
            }
            for it in r.json()
            if domain is None or it.get("entity_id", "").split(".")[0] == domain
        ]
    except _TOOL_ERRORS as e:
        return {"error": f"home_list_entities: {_error_text(e, base=base, token=token)}"}
    return {
        "entities": entities[:MAX_ENTITIES],
        "truncated": len(entities) > MAX_ENTITIES,
    }


@mcp.tool()
async def home_get_state(entity_id: str) -> dict:
    """Get one entity's full state: {entity_id, state, attributes}."""
    if not _ENTITY_ID_RE.fullmatch(entity_id):
        return {"error": f"invalid entity_id: {entity_id!r}"}
    cfg = _ha_config()
    if cfg is None:
        return dict(_CONFIG_ERROR)
    base, token = cfg
    try:
        r = await _client.get(
            f"{base}/api/states/{entity_id}", headers=_auth_headers(token)
        )
        if r.status_code == 404:
            return {"error": f"unknown entity: {entity_id}"}
        r.raise_for_status()
        data = r.json()
        return {
            "entity_id": data.get("entity_id", entity_id),
            "state": data.get("state", ""),
            "attributes": data.get("attributes", {}),
        }
    except _TOOL_ERRORS as e:
        return {"error": f"home_get_state: {_error_text(e, base=base, token=token)}"}


@mcp.tool()
async def home_call_service(
    domain: str, service: str, entity_id: str, data: dict | None = None
) -> dict:
    """Call a Home Assistant service on an entity, e.g. ("light", "turn_on", "light.kitchen", {"brightness": 128}).

    Any `entity_id` key inside `data` is ignored — the target is always the
    validated `entity_id` parameter.
    """
    if not _DOMAIN_RE.fullmatch(domain):
        return {"error": f"invalid domain: {domain!r}"}
    if not _SERVICE_RE.fullmatch(service):
        return {"error": f"invalid service: {service!r}"}
    if not _ENTITY_ID_RE.fullmatch(entity_id):
        return {"error": f"invalid entity_id: {entity_id!r}"}
    cfg = _ha_config()
    if cfg is None:
        return dict(_CONFIG_ERROR)
    base, token = cfg
    # Strip entity_id from data so it can't silently override the validated
    # parameter value.
    data = {k: v for k, v in (data or {}).items() if k != "entity_id"}
    try:
        r = await _client.post(
            f"{base}/api/services/{domain}/{service}",
            json={"entity_id": entity_id, **data},
            headers=_auth_headers(token),
        )
        r.raise_for_status()
        changed = [
            {"entity_id": s.get("entity_id", ""), "state": s.get("state", "")}
            for s in r.json()
        ]
    except _TOOL_ERRORS as e:
        return {"error": f"home_call_service: {_error_text(e, base=base, token=token)}"}
    return {"ok": True, "changed_states": changed}


def main() -> int:
    token = os.environ.get("MCP_TOKEN", "").strip()
    if not token:
        print("home_mcp: MCP_TOKEN not set; refusing to start", file=sys.stderr)
        return 2
    host = os.environ.get("HOME_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("HOME_MCP_PORT", "8787"))

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
