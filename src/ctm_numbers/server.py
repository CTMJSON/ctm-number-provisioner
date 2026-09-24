"""FastMCP server exposing CTM number search, purchase, and configuration tools."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .auth import Token, load_token
from .client import MAX_CONCURRENCY, CTMClient, CTMError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PURCHASE_LOG = PROJECT_ROOT / "purchases.log"

TARGET_ENDPOINTS: dict[str, tuple[str, str]] = {
    "sources": ("sources", "sources"),
    "receiving_numbers": ("receiving_numbers", "receiving_numbers"),
    "queues": ("queues.json", "queues"),
    "voice_menus": ("voice_menus", "voice_menus"),
    "users": ("users", "users"),
    # Advanced routing targets.
    "conditional_routers": ("conditional_routers", "configs"),
    "geo_routes": ("geo_routes", "geo_routes"),
    "routing_tables": ("routing_tables", "routing_tables"),
    "voice_bots": ("voice_bots", "voice_bots"),
}

# How each routing target maps onto the dial_routes body. The dial_route value
# and id key were verified against the live API (see README).
ROUTE_SPECS: dict[str, tuple[str, str]] = {
    "queue_id": ("call_queue", "call_queue_id"),
    "voice_menu_id": ("voice_menu", "voice_menu_id"),
    "user_id": ("call_agent", "user_id"),
    "conditional_router_id": ("conditional_router", "conditional_router_id"),
    "geo_route_id": ("geo_config", "geo_config_id"),
    "routing_table_id": ("routing_table", "routing_table_id"),
    "voice_bot_id": ("voice_bot", "voice_bot_id"),
}

mcp = FastMCP("ctm-number-provisioner")


def _client(account_id: str | None = None, token_name: str | None = None) -> CTMClient:
    token: Token = load_token(token_name)
    return CTMClient(token=token, account_id=account_id)


def _err(exc: CTMError) -> dict[str, Any]:
    return {"status": exc.status, "error": str(exc.body), "method": exc.method, "path": exc.path}


# --------------------------------------------------------------------------- #
# whoami
# --------------------------------------------------------------------------- #


@mcp.tool()
async def whoami(account_id: str | None = None, token_name: str | None = None) -> Any:
    """Show which CTM account the server will act on, and where the token came from.

    Call this first and tell the user the resolved account name before buying
    anything: a token can map to a different account than its label suggests.
    """
    client = _client(account_id, token_name)
    try:
        aid = client.resolve_account_id(account_id)
        data = await client.get(f"/accounts/{aid}")
        account = data.get("account") if isinstance(data, dict) and "account" in data else data
        account = account if isinstance(account, dict) else {}
        return {
            "account_id": aid,
            "account_name": account.get("name"),
            "token_source": client.token.source,
            "base_url": client.base_url,
        }
    except CTMError as exc:
        return {
            "account_id": client.default_account_id,
            "token_source": client.token.source,
            **_err(exc),
        }
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


@mcp.tool()
async def search_available_numbers(
    country: str = "US",
    searchby: str | None = None,
    area_code: str | None = None,
    pattern: str | None = None,
    address: str | None = None,
    number_prefix: str | None = None,
    operator: str | None = None,
    account_id: str | None = None,
    token_name: str | None = None,
) -> Any:
    """Search available CTM tracking numbers before buying them.

    US/CA modes (searchby is inferred when omitted):
      - area_code: numbers in an area code (e.g. "443").
      - address: street or ZIP (e.g. "21201").
      - number_prefix: area code + prefix (e.g. "917563").
      - tollfree: the default when nothing else is given.

    International: pass country (e.g. "GB") with pattern, and optionally
    operator="start_with" or "includes" (default includes).

    Returns compact rows: number, friendly_name, type, region, postal_code,
    lata, sms, mms, addr_required, hipaa_friendly. Show them to the user and let
    them pick before calling buy_numbers.
    """
    kind = searchby or _infer_searchby(area_code, address, number_prefix, pattern)
    params: dict[str, Any] = {"country": country}
    if kind == "area":
        params["searchby"] = "area"
        params["areacode"] = _strip_plus(area_code)
    elif kind == "address":
        params["searchby"] = "address"
        params["address"] = address
    elif kind == "number":
        params["searchby"] = "number"
        params["number"] = _strip_plus(number_prefix)
    elif kind == "tollfree":
        params["searchby"] = "tollfree"
    elif kind == "pattern":
        params["pattern"] = pattern
        if operator:
            params["operator"] = operator
    else:
        return {"error": f"Unknown searchby '{kind}'."}

    client = _client(account_id, token_name)
    try:
        aid = client.resolve_account_id(account_id)
        data = await client.get(f"/accounts/{aid}/numbers/search.json", **params)
    except CTMError as exc:
        return {
            "account_id": account_id,
            "status": exc.status,
            "error": str(exc.body),
            "numbers": [],
        }
    finally:
        await client.aclose()
    resolved_aid = aid

    raw = (
        data
        if isinstance(data, list)
        else (data or {}).get("numbers") or (data or {}).get("results") or []
    )
    overlays: list[str] = []
    if isinstance(data, dict):
        overlays = data.get("overlays") or data.get("overlay_area_codes") or []
    rows = [_compact_available(n) for n in raw if isinstance(n, dict)]
    return {
        "account_id": resolved_aid,
        "count": len(rows),
        "overlays": overlays,
        "numbers": rows,
    }


def _infer_searchby(
    area_code: str | None, address: str | None, prefix: str | None, pattern: str | None
) -> str | None:
    if area_code:
        return "area"
    if address:
        return "address"
    if prefix:
        return "number"
    if pattern:
        return "pattern"
    return "tollfree"


def _strip_plus(value: str | None) -> str | None:
    return value.lstrip("+") if value else value


def _compact_available(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": item.get("number") or item.get("formatted"),
        "friendly_name": item.get("friendly_name") or item.get("name"),
        "type": item.get("type"),
        "region": item.get("region") or item.get("state"),
        "postal_code": item.get("postal_code"),
        "lata": item.get("lata"),
        "sms": item.get("sms"),
        "mms": item.get("mms"),
        "addr_required": item.get("addr_required"),
        "hipaa_friendly": item.get("hipaa_friendly"),
    }


# --------------------------------------------------------------------------- #
# buy
# --------------------------------------------------------------------------- #


@mcp.tool()
async def buy_numbers(
    phone_numbers: list[str] | None = None,
    area_code: str | None = None,
    quantity: int = 1,
    test: bool = False,
    dry_run: bool = True,
    account_id: str | None = None,
    token_name: str | None = None,
) -> Any:
    """Purchase CTM tracking numbers. Real purchases cost money unless test=True.

    ALWAYS call with dry_run=True first (the default), show the user the plan
    (account name, count, numbers or area code), and get explicit confirmation
    before calling again with dry_run=False. There is no undo except
    release_numbers.

    Args:
        phone_numbers: Exact numbers from search_available_numbers.
        area_code: Let CTM pick numbers in this area code (see quantity).
        quantity: How many numbers to buy in area_code mode (1-500).
        test: Buy free test numbers. Default False (real, billable).
        dry_run: Plan only, no write calls. Default True.

    Returns rows with tpn_id; feed those tpn_ids to configure_numbers.
    """
    if bool(phone_numbers) == bool(area_code):
        return {"error": "Provide exactly one of phone_numbers or area_code."}
    if not 1 <= quantity <= 500:
        return {"error": "quantity must be between 1 and 500."}

    client = _client(account_id, token_name)
    try:
        aid = client.resolve_account_id(account_id)
        resolved_aid = aid
        if phone_numbers:
            jobs = [("number", n) for n in dict.fromkeys(phone_numbers)]
        else:
            jobs = [("areacode", _strip_plus(area_code) or "")] * quantity

        if dry_run:
            name = None
            try:
                data = await client.get(f"/accounts/{aid}")
                account = data.get("account") if isinstance(data, dict) else None
                source = account or data or {}
                name = source.get("name") if isinstance(source, dict) else None
            except CTMError:
                pass
            return {
                "dry_run": True,
                "account_id": aid,
                "account_name": name,
                "count": len(jobs),
                "mode": "exact_numbers" if phone_numbers else "area_code",
                "numbers": [n for _, n in jobs] if phone_numbers else None,
                "area_code": _strip_plus(area_code),
                "test": test,
                "next": (
                    "Show this plan to the user, get explicit confirmation, "
                    "then call again with dry_run=False."
                ),
            }

        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def buy(kind: str, value: str) -> dict[str, Any]:
            body: dict[str, Any] = (
                {"phone_number": _ensure_plus(value, kind)}
                if kind == "number"
                else {"area_code": value}
            )
            if test:
                body["test"] = True
            path = (
                f"/accounts/{aid}/numbers"
                if kind == "number"
                else f"/accounts/{aid}/numbers/areacode"
            )
            async with sem:
                try:
                    res = await client.post(path, json=body)
                except CTMError as exc:
                    return {"requested": value, "status": "error", "error": str(exc.body)}
            num = (res or {}).get("number") if isinstance(res, dict) else None
            num = num if isinstance(num, dict) else (res if isinstance(res, dict) else {})
            if not num.get("id"):
                return {"requested": value, "status": "error", "error": res}
            row = {
                "requested": value,
                "status": "ok",
                "tpn_id": num.get("id"),
                "number": num.get("number"),
                "formatted": num.get("formatted"),
            }
            _log_purchase(aid, row, test)
            return row

        rows = await asyncio.gather(*(buy(k, v) for k, v in jobs))
    finally:
        await client.aclose()

    ok = [r for r in rows if r["status"] == "ok"]
    return {
        "account_id": resolved_aid,
        "test": test,
        "purchased": len(ok),
        "failed": len(rows) - len(ok),
        "tpn_ids": [r["tpn_id"] for r in ok],
        "results": rows,
    }


def _ensure_plus(value: str, kind: str) -> str:
    if kind == "number" and value and not value.startswith("+"):
        return "+" + value.lstrip("+")
    return value


def _log_purchase(account_id: str, row: dict[str, Any], test: bool) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "account": account_id,
        "tpn_id": row.get("tpn_id"),
        "number": row.get("number"),
        "test": test,
    }
    try:
        with PURCHASE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# routing targets
# --------------------------------------------------------------------------- #


@mcp.tool()
async def list_routing_targets(
    kinds: list[str] | None = None,
    search: str | None = None,
    account_id: str | None = None,
    token_name: str | None = None,
) -> Any:
    """List everything a tracking number can be attached or routed to.

    Present these options to the user and let them choose; do not pick for them.
    With more than four options, show a numbered list and have the user reply
    with a number, or use search to narrow it down.

    Args:
        kinds: Subset of ["sources", "receiving_numbers", "queues",
               "voice_menus", "users", "conditional_routers", "geo_routes",
               "routing_tables", "voice_bots"]. Default: the first four.
        search: Case-insensitive substring filter on the row's fields.
    """
    wanted = kinds or ["sources", "receiving_numbers", "queues", "voice_menus"]
    unknown = [k for k in wanted if k not in TARGET_ENDPOINTS]
    if unknown:
        return {"error": f"Unknown kinds {unknown}. Valid: {sorted(TARGET_ENDPOINTS)}"}

    client = _client(account_id, token_name)
    try:
        aid = client.resolve_account_id(account_id)
        resolved_aid = aid
        results = await asyncio.gather(
            *(
                client.fetch_all(f"/accounts/{aid}/{path}", key)
                for path, key in (TARGET_ENDPOINTS[k] for k in wanted)
            ),
            return_exceptions=True,
        )
    finally:
        await client.aclose()

    out: dict[str, Any] = {"account_id": resolved_aid}
    needle = (search or "").lower()
    for kind, items in zip(wanted, results, strict=True):
        if isinstance(items, BaseException):
            out[kind] = {"error": str(items)}
            continue
        rows = [_compact_target(kind, item) for item in items if isinstance(item, dict)]
        if needle:
            rows = [r for r in rows if needle in json.dumps(r, default=str).lower()]
        out[kind] = rows
    return out


def _compact_target(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"id": item.get("id"), "name": item.get("name")}
    if kind == "sources":
        row["filter_id"] = item.get("filter_id")
    elif kind == "receiving_numbers":
        row["number"] = item.get("display_number") or item.get("number")
    elif kind == "queues":
        row["agents"] = item.get("total_agents")
    elif kind == "users":
        row["name"] = item.get("name") or " ".join(
            filter(None, [item.get("first_name"), item.get("last_name")])
        )
        row["email"] = item.get("email")
    elif kind == "conditional_routers":
        row["rules"] = len(item.get("routing_rules") or [])
        row["route_to_type"] = item.get("route_to_type")
        row["description"] = item.get("description") or None
    elif kind == "geo_routes":
        row["route_by"] = item.get("route_by")
        row["default_state"] = item.get("default_state")
        row["description"] = item.get("description") or None
    elif kind == "routing_tables":
        row["description"] = item.get("description") or None
    elif kind == "voice_bots":
        row["description"] = item.get("description") or None
    return row


# --------------------------------------------------------------------------- #
# configure
# --------------------------------------------------------------------------- #


@mcp.tool()
async def configure_numbers(
    tpn_ids: list[str],
    name: str | None = None,
    custom_fields: dict[str, Any] | None = None,
    source_id: str | None = None,
    receiving_number_ids: list[str] | None = None,
    queue_id: str | None = None,
    voice_menu_id: str | None = None,
    user_id: str | None = None,
    conditional_router_id: str | None = None,
    geo_route_id: str | None = None,
    routing_table_id: str | None = None,
    voice_bot_id: str | None = None,
    user_no_answer_seconds: int = 25,
    user_default_action: str = "voicemail",
    route_override: dict[str, Any] | None = None,
    account_id: str | None = None,
    token_name: str | None = None,
) -> Any:
    """Apply a name, tracking source, and one call route to one or many numbers.

    Get ids from list_routing_targets and let the user choose them. Pick at most
    ONE route: receiving_number_ids, queue_id (CQU...), voice_menu_id (VOM...),
    user_id (USR..., rings an agent), or route_override (raw
    {"virtual_phone_number": {...}} dial_routes body). Pick at most ONE route:
    receiving_number_ids, queue_id (CQU...), voice_menu_id (VOM...),
    user_id (USR...), conditional_router_id (smart router), geo_route_id (GEO...),
    routing_table_id (RTT...), voice_bot_id (VBT...), or route_override.

    Args:
        tpn_ids: Tracking numbers to configure (TPN...).
        name: Label. Supports {n} (1-based index) and {number} placeholders,
              e.g. "Google Ads {n}".
        custom_fields: Custom field values to set on each number.
        source_id: Tracking source id (TSO... or numeric) to attach the numbers to.
        receiving_number_ids: RPN ids to forward calls to.
        route_override: Raw dial_routes body; only if the named routes don't fit.
    """
    routes = [
        x
        for x in (
            receiving_number_ids,
            queue_id,
            voice_menu_id,
            user_id,
            conditional_router_id,
            geo_route_id,
            routing_table_id,
            voice_bot_id,
            route_override,
        )
        if x
    ]
    if len(routes) > 1:
        return {
            "error": "Pick only one route: receiving_number_ids, queue_id, "
            "voice_menu_id, user_id, conditional_router_id, geo_route_id, "
            "routing_table_id, voice_bot_id, or route_override."
        }
    if not tpn_ids:
        return {"error": "tpn_ids is empty."}

    dial_body: dict[str, Any] | None = route_override
    if queue_id:
        dial_body = {
            "virtual_phone_number": {"dial_route": "call_queue", "call_queue_id": queue_id}
        }
    elif voice_menu_id:
        dial_body = {
            "virtual_phone_number": {"dial_route": "voice_menu", "voice_menu_id": voice_menu_id}
        }
    elif user_id:
        dial_body = {
            "virtual_phone_number": {
                "dial_route": "call_agent",
                "user_id": user_id,
                "user_default_action_label": user_default_action,
                "user_no_answer_seconds": user_no_answer_seconds,
            }
        }
    elif conditional_router_id:
        dial_body = {
            "virtual_phone_number": {
                "dial_route": "conditional_router",
                "conditional_router_id": conditional_router_id,
            }
        }
    elif geo_route_id:
        dial_body = {
            "virtual_phone_number": {"dial_route": "geo_config", "geo_config_id": geo_route_id}
        }
    elif routing_table_id:
        dial_body = {
            "virtual_phone_number": {
                "dial_route": "routing_table",
                "routing_table_id": routing_table_id,
            }
        }
    elif voice_bot_id:
        dial_body = {
            "virtual_phone_number": {"dial_route": "voice_bot", "voice_bot_id": voice_bot_id}
        }

    client = _client(account_id, token_name)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    try:
        aid = client.resolve_account_id(account_id)
        resolved_aid = aid

        async def run_step(target: list[dict[str, Any]], label: str, coro: Any) -> None:
            async with sem:
                try:
                    await coro
                    target.append({"step": label, "status": "ok"})
                except CTMError as exc:
                    target.append({"step": label, "status": "error", "error": str(exc.body)})

        async def configure(idx: int, tpn: str) -> dict[str, Any]:
            base = f"/accounts/{aid}/numbers/{tpn}"
            steps: list[dict[str, Any]] = []
            if name or custom_fields:
                body: dict[str, Any] = {}
                if name:
                    body["name"] = await _render_name(client, aid, tpn, name, idx)
                if custom_fields:
                    body["custom_fields"] = custom_fields
                await run_step(
                    steps, "update_number", client.post(f"{base}/update_number", json=body)
                )
            if source_id:
                await run_step(
                    steps,
                    "source",
                    client.post(f"/accounts/{aid}/sources/{source_id}/numbers/{tpn}/add"),
                )
            for rpn in receiving_number_ids or []:
                await run_step(
                    steps,
                    f"receiving_number:{rpn}",
                    client.post(f"{base}/receiving_numbers/{rpn}/add"),
                )
            if dial_body:
                await run_step(
                    steps, "dial_route", client.put(f"{base}/dial_routes", json=dial_body)
                )
            failed = [s for s in steps if s["status"] == "error"]
            return {"tpn_id": tpn, "status": "error" if failed else "ok", "steps": steps}

        rows = await asyncio.gather(*(configure(i, t) for i, t in enumerate(tpn_ids, 1)))
    finally:
        await client.aclose()

    return {
        "account_id": resolved_aid,
        "configured": sum(r["status"] == "ok" for r in rows),
        "with_errors": sum(r["status"] == "error" for r in rows),
        "results": rows,
    }


async def _render_name(client: CTMClient, aid: str, tpn: str, template: str, idx: int) -> str:
    if "{number}" not in template:
        return template.replace("{n}", str(idx))
    num = await client.get(f"/accounts/{aid}/numbers/{tpn}")
    # GET /numbers/{TPN} returns the number object directly; some endpoints wrap
    # it in a "number" key. Handle both (verified against the live API).
    data = num if isinstance(num, dict) else {}
    number = data.get("number") if isinstance(data.get("number"), dict) else data
    formatted = (number or {}).get("formatted") or (number or {}).get("number") or ""
    return template.replace("{n}", str(idx)).replace("{number}", formatted)


# --------------------------------------------------------------------------- #
# release
# --------------------------------------------------------------------------- #


@mcp.tool()
async def release_numbers(
    tpn_ids: list[str],
    confirm: bool = False,
    account_id: str | None = None,
    token_name: str | None = None,
) -> Any:
    """Release (delete) tracking numbers. Destructive and irreversible.

    Use this only when the user explicitly asks, typically to clean up test
    numbers. Refuses unless confirm=True; ask the user first.
    """
    if not confirm:
        return {
            "error": "Refusing to release without confirm=True. Ask the user explicitly first.",
            "would_release": tpn_ids,
        }
    if not tpn_ids:
        return {"error": "tpn_ids is empty."}

    client = _client(account_id, token_name)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    try:
        aid = client.resolve_account_id(account_id)
        resolved_aid = aid

        async def release(tpn: str) -> dict[str, Any]:
            async with sem:
                try:
                    await client.delete(f"/accounts/{aid}/numbers/{tpn}")
                    return {"tpn_id": tpn, "status": "ok"}
                except CTMError as exc:
                    return {"tpn_id": tpn, "status": "error", "error": str(exc.body)}

        rows = await asyncio.gather(*(release(t) for t in tpn_ids))
    finally:
        await client.aclose()

    return {
        "account_id": resolved_aid,
        "released": sum(r["status"] == "ok" for r in rows),
        "failed": sum(r["status"] == "error" for r in rows),
        "results": rows,
    }