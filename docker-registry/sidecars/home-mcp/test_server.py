"""Tests for the codie-home-mcp sidecar.

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

HA_BASE = "http://ha.test:8123"


def _set_ha(monkeypatch, base: str = HA_BASE, token: str = "ha-secret-token") -> None:
    monkeypatch.setenv("HA_BASE_URL", base)
    monkeypatch.setenv("HA_TOKEN", token)


def _states_payload() -> list:
    return [
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {
                "friendly_name": "Kitchen Light",
                "brightness": 254,
                "supported_color_modes": ["brightness"],
            },
        },
        {
            "entity_id": "switch.fan",
            "state": "off",
            "attributes": {"friendly_name": "Fan"},
        },
        {
            # No friendly_name at all — must be tolerated.
            "entity_id": "sensor.raw_42",
            "state": "17.5",
            "attributes": {},
        },
    ]


# --------------------------------------------------------- home_list_entities


@respx.mock
async def test_list_entities_returns_all_with_stripped_attributes(monkeypatch):
    _set_ha(monkeypatch)
    route = respx.get(f"{HA_BASE}/api/states").mock(
        return_value=httpx.Response(200, json=_states_payload())
    )

    out = await server.home_list_entities()

    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer ha-secret-token"
    assert out["truncated"] is False
    assert out["entities"] == [
        {"entity_id": "light.kitchen", "state": "on", "friendly_name": "Kitchen Light"},
        {"entity_id": "switch.fan", "state": "off", "friendly_name": "Fan"},
        {"entity_id": "sensor.raw_42", "state": "17.5", "friendly_name": ""},
    ]
    # Full attribute dumps must NOT leak through (context blow-up).
    for ent in out["entities"]:
        assert set(ent) == {"entity_id", "state", "friendly_name"}


@respx.mock
async def test_list_entities_domain_filter(monkeypatch):
    _set_ha(monkeypatch)
    respx.get(f"{HA_BASE}/api/states").mock(
        return_value=httpx.Response(200, json=_states_payload())
    )

    out = await server.home_list_entities(domain="light")

    assert [e["entity_id"] for e in out["entities"]] == ["light.kitchen"]
    assert out["truncated"] is False


@respx.mock
async def test_list_entities_capped_with_truncated_flag(monkeypatch):
    _set_ha(monkeypatch)
    monkeypatch.setattr(server, "MAX_ENTITIES", 2)
    respx.get(f"{HA_BASE}/api/states").mock(
        return_value=httpx.Response(200, json=_states_payload())
    )

    out = await server.home_list_entities()

    assert len(out["entities"]) == 2
    assert out["truncated"] is True


@respx.mock
async def test_list_entities_strips_trailing_slash_from_base_url(monkeypatch):
    _set_ha(monkeypatch, base=HA_BASE + "/")
    route = respx.get(f"{HA_BASE}/api/states").mock(
        return_value=httpx.Response(200, json=[])
    )

    out = await server.home_list_entities()

    assert route.called
    assert str(route.calls.last.request.url) == f"{HA_BASE}/api/states"
    assert out == {"entities": [], "truncated": False}


# ------------------------------------------------------------- home_get_state


@respx.mock
async def test_get_state_success_returns_full_attributes(monkeypatch):
    _set_ha(monkeypatch)
    route = respx.get(f"{HA_BASE}/api/states/light.kitchen").mock(
        return_value=httpx.Response(200, json=_states_payload()[0])
    )

    out = await server.home_get_state("light.kitchen")

    assert route.called
    assert route.calls.last.request.headers["Authorization"] == (
        "Bearer ha-secret-token"
    )
    assert out == {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {
            "friendly_name": "Kitchen Light",
            "brightness": 254,
            "supported_color_modes": ["brightness"],
        },
    }


@respx.mock
async def test_get_state_404_returns_unknown_entity_error(monkeypatch):
    _set_ha(monkeypatch)
    respx.get(f"{HA_BASE}/api/states/light.nope").mock(
        return_value=httpx.Response(404, json={"message": "Entity not found."})
    )

    out = await server.home_get_state("light.nope")

    assert out == {"error": "unknown entity: light.nope"}


# ---------------------------------------------------------- home_call_service


@respx.mock
async def test_call_service_posts_merged_body_and_maps_changed_states(monkeypatch):
    _set_ha(monkeypatch)
    route = respx.post(f"{HA_BASE}/api/services/light/turn_on").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "entity_id": "light.kitchen",
                    "state": "on",
                    "attributes": {"brightness": 128},
                }
            ],
        )
    )

    out = await server.home_call_service(
        "light", "turn_on", "light.kitchen", data={"brightness": 128}
    )

    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer ha-secret-token"
    assert json.loads(req.content) == {
        "entity_id": "light.kitchen",
        "brightness": 128,
    }
    assert out == {
        "ok": True,
        "changed_states": [{"entity_id": "light.kitchen", "state": "on"}],
    }


@respx.mock
async def test_call_service_without_extra_data(monkeypatch):
    _set_ha(monkeypatch)
    route = respx.post(f"{HA_BASE}/api/services/switch/toggle").mock(
        return_value=httpx.Response(200, json=[])
    )

    out = await server.home_call_service("switch", "toggle", "switch.fan")

    assert json.loads(route.calls.last.request.content) == {
        "entity_id": "switch.fan"
    }
    assert out == {"ok": True, "changed_states": []}


@respx.mock
async def test_call_service_ha_400_returns_error_with_status(monkeypatch):
    _set_ha(monkeypatch)
    respx.post(f"{HA_BASE}/api/services/light/turn_on").mock(
        return_value=httpx.Response(400, json={"message": "no such service"})
    )

    out = await server.home_call_service("light", "turn_on", "light.kitchen")

    assert set(out) == {"error"}
    assert "400" in out["error"]
    assert "ha-secret-token" not in out["error"]


# ------------------------------------------------------------ shape validation


@respx.mock
async def test_call_service_bad_domain_rejected_without_http(monkeypatch):
    _set_ha(monkeypatch)
    catch_all = respx.route().mock(return_value=httpx.Response(200, json=[]))

    out = await server.home_call_service("light/../hack", "turn_on", "light.kitchen")

    assert set(out) == {"error"}
    assert not catch_all.called


@respx.mock
async def test_call_service_bad_service_rejected_without_http(monkeypatch):
    _set_ha(monkeypatch)
    catch_all = respx.route().mock(return_value=httpx.Response(200, json=[]))

    out = await server.home_call_service("light", "turn-on!", "light.kitchen")

    assert set(out) == {"error"}
    assert not catch_all.called


@respx.mock
async def test_call_service_bad_entity_id_rejected_without_http(monkeypatch):
    _set_ha(monkeypatch)
    catch_all = respx.route().mock(return_value=httpx.Response(200, json=[]))

    out = await server.home_call_service("light", "turn_on", "light/../hack")

    assert set(out) == {"error"}
    assert not catch_all.called


@respx.mock
async def test_get_state_bad_entity_id_rejected_without_http(monkeypatch):
    _set_ha(monkeypatch)
    catch_all = respx.route().mock(return_value=httpx.Response(200, json={}))

    out = await server.home_get_state("../config")

    assert set(out) == {"error"}
    assert not catch_all.called


@respx.mock
async def test_get_state_trailing_newline_rejected_without_http(monkeypatch):
    # re.match would accept "light.kitchen\n" ($ matches before a trailing
    # newline); fullmatch must reject it before any URL is built.
    _set_ha(monkeypatch)
    catch_all = respx.route().mock(return_value=httpx.Response(200, json={}))

    out = await server.home_get_state("light.kitchen\n")

    assert set(out) == {"error"}
    assert not catch_all.called


@respx.mock
async def test_call_service_data_cannot_override_entity_id(monkeypatch):
    # A malicious/confused `data` payload carrying its own entity_id must not
    # displace the validated entity_id parameter in the POST body.
    _set_ha(monkeypatch)
    route = respx.post(f"{HA_BASE}/api/services/lock/unlock").mock(
        return_value=httpx.Response(200, json=[])
    )

    out = await server.home_call_service(
        "lock", "unlock", "lock.front_door", data={"entity_id": "lock.other"}
    )

    assert json.loads(route.calls.last.request.content) == {
        "entity_id": "lock.front_door"
    }
    assert out == {"ok": True, "changed_states": []}


# ------------------------------------------------------------- config missing


@pytest.mark.parametrize("missing", ["HA_BASE_URL", "HA_TOKEN"])
async def test_missing_config_returns_error(monkeypatch, missing):
    _set_ha(monkeypatch)
    monkeypatch.delenv(missing, raising=False)

    expected = {"error": "HA_BASE_URL/HA_TOKEN not configured"}
    assert await server.home_list_entities() == expected
    assert await server.home_get_state("light.kitchen") == expected
    assert await server.home_call_service("light", "turn_on", "light.kitchen") == (
        expected
    )


# ------------------------------------------------------------- error contract


@respx.mock
async def test_ha_unreachable_mentions_base_url_not_token(monkeypatch):
    _set_ha(monkeypatch)
    respx.get(f"{HA_BASE}/api/states").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    out = await server.home_list_entities()

    assert set(out) == {"error"}
    assert HA_BASE in out["error"]
    assert "unreachable" in out["error"]
    assert "ha-secret-token" not in out["error"]


@respx.mock
async def test_ha_500_returns_error_dict(monkeypatch):
    _set_ha(monkeypatch)
    respx.get(f"{HA_BASE}/api/states").mock(
        return_value=httpx.Response(500, text="boom")
    )

    out = await server.home_list_entities()

    assert set(out) == {"error"}
    assert "500" in out["error"]
    assert "ha-secret-token" not in out["error"]


@respx.mock
async def test_ha_non_json_response_returns_error_dict(monkeypatch):
    _set_ha(monkeypatch)
    respx.get(f"{HA_BASE}/api/states").mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )

    out = await server.home_list_entities()

    assert set(out) == {"error"}


@respx.mock
async def test_ha_scalar_json_response_returns_error_dict(monkeypatch):
    # HA returning a bare JSON scalar where a list is expected (TypeError on
    # iteration) must stay inside the uniform {"error": ...} contract.
    _set_ha(monkeypatch)
    respx.post(f"{HA_BASE}/api/services/light/turn_on").mock(
        return_value=httpx.Response(200, json=5)
    )

    out = await server.home_call_service("light", "turn_on", "light.kitchen")

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
