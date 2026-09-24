"""Offline tests for the CTM number provisioner tools (respx, no live calls)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ctm_numbers import server
from ctm_numbers.auth import AuthError, Token, load_token, parse_env_file
from ctm_numbers.client import CTMClient

BASE = "https://api.calltrackingmetrics.com/api/v1"
AID = "12345"


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr("ctm_numbers.client.BACKOFF_SECONDS", (0.0, 0.0, 0.0))


@pytest.fixture(autouse=True)
def _stub_client(monkeypatch, tmp_path):
    """Give the server a real httpx client (respx intercepts transport) and a temp log."""

    def factory(account_id=None, token_name=None):
        return CTMClient(token=Token("dGVzdA==", "env"), account_id=account_id or AID)

    monkeypatch.setattr(server, "_client", factory)
    monkeypatch.setattr(server, "PURCHASE_LOG", tmp_path / "purchases.log")


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


def test_env_var_wins(monkeypatch, tmp_path):
    env_file = tmp_path / "env.txt"
    env_file.write_text("acct:from-file\n")
    monkeypatch.setenv("CTM_BASIC_AUTH", "from-env")
    token = load_token("acct", env_file)
    assert token.value == "from-env"
    assert token.source == "env"


def test_env_file_parsing(monkeypatch, tmp_path):
    monkeypatch.delenv("CTM_BASIC_AUTH", raising=False)
    env_file = tmp_path / "env.txt"
    env_file.write_text(
        "# comment\n\nacct:abc:def\nother:zzz\n",
    )
    token = load_token("acct", env_file)
    assert token.value == "abc:def"
    assert token.source == "env.txt:acct"


def test_env_file_missing_name(tmp_path):
    env_file = tmp_path / "env.txt"
    env_file.write_text("other:zzz\n")
    with pytest.raises(AuthError, match="nope"):
        parse_env_file(env_file, "nope")


def test_missing_credentials_is_actionable(monkeypatch):
    monkeypatch.delenv("CTM_BASIC_AUTH", raising=False)
    monkeypatch.delenv("CTM_TOKEN_NAME", raising=False)
    with pytest.raises(AuthError, match="No CTM credentials configured"):
        load_token()


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


@respx.mock
async def test_search_area_params():
    route = respx.get(f"{BASE}/accounts/{AID}/numbers/search.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "numbers": [
                    {
                        "number": "+14435551234",
                        "friendly_name": "443-555-1234",
                        "type": "local",
                        "region": "MD",
                        "postal_code": "21201",
                        "lata": "238",
                        "sms": True,
                        "mms": False,
                        "addr_required": False,
                        "hipaa_friendly": True,
                    }
                ],
                "overlays": ["410"],
            },
        )
    )
    out = await server.search_available_numbers(area_code="443")
    assert route.calls[0].request.url.params["searchby"] == "area"
    assert route.calls[0].request.url.params["areacode"] == "443"
    assert out["count"] == 1
    assert out["overlays"] == ["410"]
    assert out["numbers"][0]["sms"] is True


@respx.mock
async def test_search_infers_modes():
    route = respx.get(f"{BASE}/accounts/{AID}/numbers/search.json").mock(
        return_value=httpx.Response(200, json={"numbers": []})
    )
    await server.search_available_numbers(address="21201")
    assert route.calls[0].request.url.params["searchby"] == "address"
    await server.search_available_numbers(number_prefix="917563")
    assert route.calls[1].request.url.params["searchby"] == "number"
    await server.search_available_numbers()
    assert route.calls[2].request.url.params["searchby"] == "tollfree"
    await server.search_available_numbers(country="GB", pattern="430", operator="start_with")
    assert route.calls[3].request.url.params["pattern"] == "430"
    assert route.calls[3].request.url.params["operator"] == "start_with"
    assert "searchby" not in route.calls[3].request.url.params


# --------------------------------------------------------------------------- #
# buy
# --------------------------------------------------------------------------- #


@respx.mock
async def test_buy_dry_run_makes_no_posts():
    respx.get(f"{BASE}/accounts/{AID}").mock(
        return_value=httpx.Response(200, json={"name": "Acme Tracking"})
    )
    posts = respx.post(url__regex=rf"{BASE}/accounts/{AID}/numbers.*")
    out = await server.buy_numbers(area_code="443", quantity=3)
    assert out["dry_run"] is True
    assert out["count"] == 3
    assert posts.call_count == 0


@respx.mock
async def test_buy_both_or_neither_errors():
    assert "error" in await server.buy_numbers()
    assert "error" in await server.buy_numbers(phone_numbers=["+1"], area_code="443")


@respx.mock
async def test_buy_area_code_posts_quantity_times():
    route = respx.post(f"{BASE}/accounts/{AID}/numbers/areacode").mock(
        side_effect=[
            httpx.Response(200, json={"number": {"id": "TPN1", "number": "111"}}),
            httpx.Response(200, json={"number": {"id": "TPN2", "number": "222"}}),
        ]
    )
    out = await server.buy_numbers(area_code="443", quantity=2, test=True, dry_run=False)
    assert route.call_count == 2
    for call in route.calls:
        assert json.loads(call.request.content) == {"area_code": "443", "test": True}
    assert out["purchased"] == 2
    assert out["tpn_ids"] == ["TPN1", "TPN2"]


@respx.mock
async def test_buy_batch_survives_one_failure():
    respx.post(f"{BASE}/accounts/{AID}/numbers").mock(
        side_effect=[
            httpx.Response(200, json={"number": {"id": "TPN1", "number": "111"}}),
            httpx.Response(400, json={"error": "unavailable"}),
        ]
    )
    out = await server.buy_numbers(
        phone_numbers=["+15550000001", "+15550000002"], dry_run=False
    )
    assert out["purchased"] == 1
    assert out["failed"] == 1
    assert out["results"][1]["status"] == "error"


@respx.mock
async def test_buy_writes_audit_log(tmp_path):
    log = tmp_path / "purchases.log"
    server.PURCHASE_LOG = log
    respx.post(f"{BASE}/accounts/{AID}/numbers/areacode").mock(
        return_value=httpx.Response(200, json={"number": {"id": "TPN9", "number": "999"}})
    )
    await server.buy_numbers(area_code="443", quantity=1, test=True, dry_run=False)
    entry = json.loads(log.read_text().strip())
    assert entry["tpn_id"] == "TPN9"
    assert entry["account"] == AID
    assert entry["test"] is True


# --------------------------------------------------------------------------- #
# list_routing_targets
# --------------------------------------------------------------------------- #


@respx.mock
async def test_list_fetches_remaining_pages():
    respx.get(f"{BASE}/accounts/{AID}/sources").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "sources": [
                    {
                        "id": f"TSO{request.url.params['page']}",
                        "name": "src",
                        "filter_id": 5,
                    }
                ],
                "total_pages": 3,
            },
        )
    )
    out = await server.list_routing_targets(kinds=["sources"])
    assert len(out["sources"]) == 3
    assert out["sources"][0]["filter_id"] == 5


@respx.mock
async def test_list_search_filters():
    respx.get(f"{BASE}/accounts/{AID}/sources").mock(
        return_value=httpx.Response(
            200,
            json={
                "sources": [
                    {"id": "TSO1", "name": "Google Ads", "filter_id": 1},
                    {"id": "TSO2", "name": "Facebook", "filter_id": 2},
                ],
                "total_pages": 1,
            },
        )
    )
    out = await server.list_routing_targets(kinds=["sources"], search="google")
    assert [r["id"] for r in out["sources"]] == ["TSO1"]


# --------------------------------------------------------------------------- #
# configure
# --------------------------------------------------------------------------- #


@respx.mock
async def test_configure_rejects_multiple_routes():
    out = await server.configure_numbers(["TPN1"], queue_id="CQU1", voice_menu_id="VOM1")
    assert "error" in out


@respx.mock
async def test_configure_route_bodies():
    dial = respx.put(url__regex=rf"{BASE}/accounts/{AID}/numbers/TPN1/dial_routes").mock(
        return_value=httpx.Response(200, json={})
    )
    await server.configure_numbers(["TPN1"], queue_id="CQU1")
    assert json.loads(dial.calls[0].request.content) == {
        "virtual_phone_number": {"dial_route": "call_queue", "call_queue_id": "CQU1"}
    }
    await server.configure_numbers(["TPN1"], voice_menu_id="VOM1")
    assert json.loads(dial.calls[1].request.content) == {
        "virtual_phone_number": {"dial_route": "voice_menu", "voice_menu_id": "VOM1"}
    }
    await server.configure_numbers(["TPN1"], user_id="USR1")
    body = json.loads(dial.calls[2].request.content)["virtual_phone_number"]
    assert body["dial_route"] == "call_agent"
    assert body["user_id"] == "USR1"
    assert body["user_no_answer_seconds"] == 25


@respx.mock
async def test_configure_name_placeholders():
    # Live GET /numbers/{TPN} returns the number object directly (no wrapper).
    for tpn, formatted in (("TPN1", "(443) 555-0001"), ("TPN2", "(443) 555-0002")):
        respx.get(f"{BASE}/accounts/{AID}/numbers/{tpn}").mock(
            return_value=httpx.Response(
                200, json={"id": tpn, "number": "+14435550001", "formatted": formatted}
            )
        )
    update = respx.post(url__regex=rf"{BASE}/accounts/{AID}/numbers/TPN[12]/update_number").mock(
        return_value=httpx.Response(200, json={})
    )
    out = await server.configure_numbers(["TPN1", "TPN2"], name="Ads {n} {number}")
    assert out["with_errors"] == 0
    bodies = sorted(json.loads(c.request.content)["name"] for c in update.calls)
    assert bodies == ["Ads 1 (443) 555-0001", "Ads 2 (443) 555-0002"]


@respx.mock
async def test_configure_uses_tso_source_id():
    add = respx.post(f"{BASE}/accounts/{AID}/sources/TSO1/numbers/TPN1/add").mock(
        return_value=httpx.Response(200, json={})
    )
    out = await server.configure_numbers(["TPN1"], source_id="TSO1")
    assert out["with_errors"] == 0
    assert add.call_count == 1


@respx.mock
async def test_configure_captures_step_errors():
    respx.post(f"{BASE}/accounts/{AID}/numbers/TPN1/receiving_numbers/RPN1/add").mock(
        return_value=httpx.Response(404, json={"error": "nope"})
    )
    out = await server.configure_numbers(["TPN1"], receiving_number_ids=["RPN1"])
    assert out["with_errors"] == 1
    assert out["results"][0]["steps"][0]["status"] == "error"


@respx.mock
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"conditional_router_id": "196"},
            {"dial_route": "conditional_router", "conditional_router_id": "196"},
        ),
        (
            {"geo_route_id": "GEO123"},
            {"dial_route": "geo_config", "geo_config_id": "GEO123"},
        ),
        (
            {"routing_table_id": "RTT123"},
            {"dial_route": "routing_table", "routing_table_id": "RTT123"},
        ),
        (
            {"voice_bot_id": "VBT123"},
            {"dial_route": "voice_bot", "voice_bot_id": "VBT123"},
        ),
    ],
)
async def test_configure_advanced_route_bodies(kwargs, expected):
    dial = respx.put(f"{BASE}/accounts/{AID}/numbers/TPN1/dial_routes").mock(
        return_value=httpx.Response(200, json={})
    )
    out = await server.configure_numbers(["TPN1"], **kwargs)
    assert out["with_errors"] == 0
    assert json.loads(dial.calls[0].request.content) == {"virtual_phone_number": expected}


@respx.mock
async def test_configure_rejects_multiple_advanced_routes():
    out = await server.configure_numbers(["TPN1"], queue_id="CQU1", voice_bot_id="VBT1")
    assert "error" in out
    assert "queue_id" in out["error"] and "voice_bot_id" in out["error"]


@respx.mock
async def test_list_advanced_targets():
    respx.get(f"{BASE}/accounts/{AID}/conditional_routers").mock(
        return_value=httpx.Response(
            200,
            json={
                "configs": [
                    {"id": 196, "name": "New Female Callers", "routing_rules": [1, 2]}
                ],
                "total_pages": 1,
            },
        )
    )
    respx.get(f"{BASE}/accounts/{AID}/geo_routes").mock(
        return_value=httpx.Response(
            200,
            json={
                "geo_routes": [{"id": "GEO1", "name": "Main Line", "route_by": "zipcode"}],
                "total_pages": 1,
            },
        )
    )
    respx.get(f"{BASE}/accounts/{AID}/voice_bots").mock(
        return_value=httpx.Response(
            200,
            json={"voice_bots": [{"id": "VBT1", "name": "Receptionist"}], "total_pages": 1},
        )
    )
    respx.get(f"{BASE}/accounts/{AID}/routing_tables").mock(
        return_value=httpx.Response(204)
    )
    out = await server.list_routing_targets(
        kinds=["conditional_routers", "geo_routes", "voice_bots", "routing_tables"]
    )
    assert out["conditional_routers"][0] == {
        "id": 196,
        "name": "New Female Callers",
        "rules": 2,
        "route_to_type": None,
        "description": None,
    }
    assert out["geo_routes"][0]["route_by"] == "zipcode"
    assert out["voice_bots"][0]["id"] == "VBT1"
    assert out["routing_tables"] == []


# --------------------------------------------------------------------------- #
# release
# --------------------------------------------------------------------------- #


@respx.mock
async def test_release_requires_confirm():
    out = await server.release_numbers(["TPN1"])
    assert "error" in out
    assert out["would_release"] == ["TPN1"]


# --------------------------------------------------------------------------- #
# retry
# --------------------------------------------------------------------------- #


@respx.mock
async def test_429_is_retried():
    route = respx.get(f"{BASE}/accounts/{AID}/numbers/search.json").mock(
        side_effect=[
            httpx.Response(429, json={"error": "slow down"}),
            httpx.Response(200, json={"numbers": []}),
        ]
    )
    out = await server.search_available_numbers(area_code="443")
    assert route.call_count == 2
    assert out["count"] == 0