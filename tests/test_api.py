"""Standalone tests for the Deliveroo API client (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

API_PATH = Path(__file__).parents[1] / "custom_components" / "deliveroo" / "api.py"
spec = importlib.util.spec_from_file_location("deliveroo_api", API_PATH)
api = importlib.util.module_from_spec(spec)
sys.modules["deliveroo_api"] = api
spec.loader.exec_module(api)


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def make_jwt(exp: int, iss: str = "rooconsumerauth") -> str:
    header = _b64({"alg": "ES256", "typ": "JWT"})
    payload = _b64({"cust": 123, "drn_id": "x", "exp": exp, "iss": iss, "sess": "web,abc"})
    return f"{header}.{payload}.c2lnbmF0dXJl"


def make_page(*, logged_in: bool = True, bearer_exp: int = 2_000_000_000) -> str:
    jwt = make_jwt(bearer_exp)
    state = {
        "user": {"isLoggedIn": logged_in, "id": 123, "firstName": "Alex", "preferredName": "Alex"},
        "request": {"apiAuth": {"token": jwt}},
        "auth": {"other": make_jwt(bearer_exp - 100), "foreign": make_jwt(bearer_exp + 999, iss="other")},
        "order": {
            "history": {
                "orders": [
                    {"id": 50000000191, "status": "CONFIRMED", "consumerStatusCode": "PROCESSING",
                     "statusText": "Confirmed", "restaurantName": "Trattoria Demo"},
                    {"id": 51100000001, "status": "DELIVERED", "consumerStatusCode": "COMPLETE",
                     "statusText": "Delivered", "restaurantName": "KFC "},
                ]
            }
        },
    }
    next_data = json.dumps({"props": {"initialState": state}, "page": "/order-history"})
    return (
        "<html><body><div id='__next'></div>"
        f'<script id="__NEXT_DATA__" type="application/json">{next_data}</script>'
        "</body></html>"
    )


STATUS_PAYLOAD = {
    "data": {
        "type": "consumer_order_status",
        "attributes": {
            "ui_status": "PROCESSING",
            "message": "Trattoria Demo sta preparando il tuo ordine",
            "eta_message": "20:20–20:50",
            "eta_status_code": "ON_TIME",
            "current_progress_percentage": 49,
            "rider_route": "TO_RESTAURANT",
            "rider_validation_code": 78,
            "rider_validation_code_s": "78",
            "is_completed": False,
            "is_failed": False,
            "updated_at": "2026-09-27 20:03:02 +0200",
        },
    },
    "included": [
        {"type": "order", "attributes": {"order_number": "0191", "sharing_token": "SHARE123",
                                         "restaurant_name": "Trattoria Demo"}},
        {"type": "customer", "attributes": {"address": "redacted"}},
    ],
}


def test_parse_orders_page() -> None:
    account = api.parse_orders_page(make_page())
    assert account.customer_id == "123"
    assert account.name == "Alex"
    assert account.bearer_expires == 2_000_000_000  # newest rooconsumerauth token wins
    assert api.decode_jwt_payload(account.bearer.split(".")[1])["iss"] == "rooconsumerauth"
    assert [o.id for o in account.active_orders] == ["50000000191"]
    assert account.orders[1].restaurant_name == "KFC"


def test_parse_orders_page_logged_out() -> None:
    with pytest.raises(api.DeliverooAuthError):
        api.parse_orders_page(make_page(logged_in=False))


def test_parse_orders_page_blocked() -> None:
    with pytest.raises(api.DeliverooBlockedError):
        api.parse_orders_page("<html>Access denied</html>")


def test_parse_status() -> None:
    status = api.parse_order_status("50000000191", STATUS_PAYLOAD)
    assert status.state == "processing"
    assert status.eta == "20:20–20:50"
    assert status.progress == 49
    assert status.rider_code == "78"
    assert status.sharing_token == "SHARE123"
    assert status.restaurant_name == "Trattoria Demo"

    done = json.loads(json.dumps(STATUS_PAYLOAD))
    done["data"]["attributes"]["is_completed"] = True
    assert api.parse_order_status("1", done).state == "completed"


def test_client_against_fake_server(socket_enabled) -> None:
    asyncio.run(_client_flow())


async def _client_flow() -> None:
    seen: dict[str, dict] = {}

    async def orders(request: web.Request) -> web.Response:
        seen["orders"] = dict(request.headers)
        cookie = request.cookies.get("consumer_auth_token")
        if cookie not in ("OLD", "NEW"):
            return web.Response(text=make_page(logged_in=False), content_type="text/html")
        resp = web.Response(text=make_page(), content_type="text/html")
        resp.set_cookie("consumer_auth_token", "NEW", httponly=True)
        return resp

    async def status(request: web.Request) -> web.Response:
        seen["status"] = {"headers": dict(request.headers), "query": dict(request.query)}
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") and request.query.get("sharing_token") != "SHARE123":
            return web.Response(status=401)
        return web.Response(text=json.dumps(STATUS_PAYLOAD), content_type="application/vnd.api+json")

    app = web.Application()
    app.router.add_get("/it/orders", orders)
    app.router.add_get("/consumer/v2-6/consumer_order_statuses/{order_id}", status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    api.MARKETS["test"] = {"web": base, "api": base, "lang": "it"}

    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
            client = api.DeliverooClient(session, "test", " OLD ", "Europe/Rome")
            account = await client.async_get_account()
            assert client.token == "NEW"  # rotated cookie picked up
            assert "Chrome" in seen["orders"]["User-Agent"]

            st = await client.async_get_order_status("50000000191", bearer=account.bearer)
            assert st.message.startswith("Trattoria Demo")
            assert seen["status"]["query"]["tz"] == "Europe/Rome"
            assert seen["status"]["headers"]["Authorization"].startswith("Bearer eyJ")

            st2 = await client.async_get_order_status("50000000191", sharing_token="SHARE123")
            assert "Authorization" not in seen["status"]["headers"]
            assert st2.progress == 49

            with pytest.raises(api.DeliverooAuthError):
                await client.async_get_order_status("1", sharing_token="WRONG")

            bad = api.DeliverooClient(session, "test", "EXPIRED", "Europe/Rome")
            with pytest.raises(api.DeliverooAuthError):
                await bad.async_get_account()
    finally:
        await runner.cleanup()
        api.MARKETS.pop("test", None)


def test_parse_status_real_in_transit_payload() -> None:
    """Shape taken from a real order while the rider was on the way."""
    payload = {
        "data": {
            "attributes": {
                "eta_message": "19:52–20:20",
                "message": "Il tuo ordine è stato ritirato",
                "advisory": "Il rider ha un altro ordine da consegnare lungo il tragitto.",
                "ui_status": "PROCESSING",
                "eta_status_code": "ON_TIME",
                "is_failed": False,
                "is_completed": False,
                "processing_steps": [
                    {"title": "Ordine inviato", "ends_at_progress_percentage": 10, "is_current": False},
                    {"title": "Attesa conferma", "ends_at_progress_percentage": 30, "is_current": False},
                    {"title": "In preparazione", "ends_at_progress_percentage": 70, "is_current": False},
                    {"title": "In transito", "ends_at_progress_percentage": 90, "is_current": True},
                    {"title": "Il tuo rider è vicino!", "ends_at_progress_percentage": 100, "is_current": False},
                ],
                "analytics": {
                    "status_message": "Delivering",
                    "estimated_delivery_time": "2026-10-04T18:03:18Z",
                    "rider_status": "en-route",
                },
                "rider_route": "TO_CUSTOMER",
                "current_progress_percentage": 70,
            }
        }
    }
    status = api.parse_order_status("50000000931", payload)
    assert status.state == "processing"
    assert status.rider_route == "TO_CUSTOMER"
    assert status.step_index == 4
    assert status.step_title == "In transito"
    assert len(status.steps) == 5
    assert status.rider_status == "en-route"
    assert status.advisory.startswith("Il rider")
    assert status.estimated_delivery.isoformat() == "2026-10-04T18:03:18+00:00"


def test_parse_status_tolerates_missing_or_bad_fields() -> None:
    status = api.parse_order_status("1", {"data": {"attributes": {
        "processing_steps": None, "analytics": "oops", "advisory": "  "}}})
    assert status.steps == []
    assert status.step_index is None and status.step_title is None
    assert status.estimated_delivery is None and status.advisory is None
    bad = api.parse_order_status("1", {"data": {"attributes": {
        "analytics": {"estimated_delivery_time": "not-a-date"}}}})
    assert bad.estimated_delivery is None


def test_parse_order_list() -> None:
    assert api.parse_order_list({"orders": [], "count": 0}) == []
    orders = api.parse_order_list({
        "count": 1,
        "orders": [
            {"id": 50000000931, "status": "CONFIRMED", "consumer_status": "PROCESSING",
             "restaurant": {"name": " Trattoria Demo "}, "order_number": "0931"},
            {"status": "no id, skipped"},
            "garbage",
        ],
    })
    assert len(orders) == 1
    assert orders[0].id == "50000000931"
    assert orders[0].restaurant_name == "Trattoria Demo"
    assert orders[0].consumer_status_code == "PROCESSING"
    # unexpected shapes never raise
    assert api.parse_order_list(None) == []
    assert api.parse_order_list({"orders": None}) == []
    odd = api.parse_order_list({"orders": [{"id": 1, "restaurant": "x", "consumer_status": {"a": 1}}]})
    assert odd[0].restaurant_name is None and odd[0].consumer_status_code is None


def test_active_orders_against_fake_server(socket_enabled) -> None:
    asyncio.run(_active_orders_flow())


async def _active_orders_flow() -> None:
    seen: dict = {}

    async def orders(request: web.Request) -> web.Response:
        seen["headers"] = dict(request.headers)
        seen["query"] = dict(request.query)
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer GOOD":
            return web.json_response({"orders": [{"id": 7, "restaurant": {"name": "Demo"}}], "count": 1})
        if auth == "Bearer LIMITED":
            return web.Response(status=429)
        return web.Response(status=401)

    app = web.Application()
    app.router.add_get("/consumer/order-history/v1/orders", orders)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    api.MARKETS["test"] = {"web": base, "api": base, "lang": "it"}
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
            client = api.DeliverooClient(session, "test", "COOKIE", "Europe/Rome")
            result = await client.async_get_active_orders("GOOD")
            assert [o.id for o in result] == ["7"]
            assert seen["query"] == {"state": "active"}
            assert seen["headers"]["X-Roo-RequestSource"] == "orders"
            assert "Cookie" not in seen["headers"]  # the session cookie never goes to the API host
            with pytest.raises(api.DeliverooAuthError):
                await client.async_get_active_orders("EXPIRED")
            with pytest.raises(api.DeliverooBlockedError):
                await client.async_get_active_orders("LIMITED")
    finally:
        await runner.cleanup()
        api.MARKETS.pop("test", None)
