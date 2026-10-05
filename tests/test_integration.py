"""Home Assistant level tests: config flow, reauth, coordinator lifecycle."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.deliveroo.api import (
    DeliverooAccount,
    DeliverooAuthError,
    DeliverooBlockedError,
    DeliverooConnectionError,
    DeliverooError,
    DeliverooOrder,
    DeliverooOrderStatus,
    DeliverooStep,
    DeliverooUser,
)
from custom_components.deliveroo.const import (
    CONF_ACTIVE_INTERVAL,
    CONF_IDLE_INTERVAL,
    CONF_MARKET,
    CONF_TOKEN,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    EVENT_ORDER_UPDATE,
    FALLBACK_IDLE_INTERVAL,
    SESSION_REFRESH,
)

CLIENT = "custom_components.deliveroo.api.DeliverooClient"
ACTIVE_INTERVAL = timedelta(seconds=DEFAULT_ACTIVE_INTERVAL)
IDLE_INTERVAL = timedelta(seconds=DEFAULT_IDLE_INTERVAL)

ACTIVE_ORDER = DeliverooOrder("222", "CONFIRMED", "PROCESSING", None, "Trattoria Demo")
USER = DeliverooUser(customer_id="123", name="Alex")


def make_token(cust: int = 123) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'ES256'})}.{b64({'cust': cust, 'iss': 'rooconsumerauth'})}.c2ln"


TOKEN = make_token()


def account(active: bool = True) -> DeliverooAccount:
    orders = [
        DeliverooOrder("111", "DELIVERED", "COMPLETE", "Delivered", "KFC"),
    ]
    if active:
        orders.insert(0, DeliverooOrder("222", "CONFIRMED", "PROCESSING", "Confirmed", "Trattoria Demo"))
    return DeliverooAccount(
        customer_id="123",
        name="Alex",
        bearer="eyJ.eyJ.sig",
        bearer_expires=dt_util.utcnow().timestamp() + 3600,
        orders=orders,
    )


def status(**kw) -> DeliverooOrderStatus:
    base = dict(
        order_id="222",
        ui_status="PROCESSING",
        message="Trattoria Demo sta preparando il tuo ordine",
        eta="20:20–20:50",
        eta_status="ON_TIME",
        progress=49,
        rider_route="TO_RESTAURANT",
        rider_code="78",
        is_completed=False,
        is_failed=False,
        restaurant_name="Trattoria Demo",
        order_number="0191",
        sharing_token="SHARE",
        updated_at=None,
    )
    base.update(kw)
    return DeliverooOrderStatus(**base)


@pytest.fixture
def api():
    """Patch the client calls for the whole test.

    Defaults: valid token, one active order, status "processing".
    ``account`` is the website page (keep-alive / fallback / token recovery).
    """
    mocks = SimpleNamespace(
        user=AsyncMock(return_value=USER),
        account=AsyncMock(return_value=account()),
        active=AsyncMock(return_value=[ACTIVE_ORDER]),
        status=AsyncMock(return_value=status()),
    )
    with (
        patch(f"{CLIENT}.async_get_user", mocks.user),
        patch(f"{CLIENT}.async_get_account", mocks.account),
        patch(f"{CLIENT}.async_get_active_orders", mocks.active),
        patch(f"{CLIENT}.async_get_order_status", mocks.status),
    ):
        yield mocks


def rotate_token_on_page_read(new_token: str, **account_kw):
    """Page mock that behaves like Deliveroo handing out a new cookie."""

    async def fake_get_account(self):
        self._token = new_token
        return account(**account_kw)

    return fake_get_account


async def setup_entry(hass: HomeAssistant, **kw) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="it_123",
        title="Deliveroo",
        data={CONF_MARKET: "it", CONF_TOKEN: "COOKIE"},
        **kw,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def tick(hass: HomeAssistant, after: timedelta) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + after)
    await hass.async_block_till_done()


# ── Config flow ──────────────────────────────────────────────────────────────


async def test_user_flow_success(hass: HomeAssistant, api) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patch("custom_components.deliveroo.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            # pasted with the cookie name, quotes and spaces: must be cleaned up
            {CONF_MARKET: "it", CONF_TOKEN: f'  consumer_auth_token="{TOKEN}"; Path=/ '},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Deliveroo (Alex)"
    assert result["data"] == {CONF_MARKET: "it", CONF_TOKEN: TOKEN}
    assert result["result"].unique_id == "it_123"
    assert api.account.await_count == 0  # the website is never contacted during setup


async def test_user_flow_errors(hass: HomeAssistant, api) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    # Not a JWT at all: rejected without any network call
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MARKET: "it", CONF_TOKEN: "not-the-right-cookie"}
    )
    assert result["errors"] == {"base": "invalid_format"}
    assert api.user.await_count == 0

    for exc, err in (
        (DeliverooAuthError(), "invalid_auth"),
        (DeliverooBlockedError(), "blocked"),
        (DeliverooConnectionError(), "cannot_connect"),
    ):
        api.user.side_effect = exc
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MARKET: "it", CONF_TOKEN: TOKEN}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": err}


async def test_expired_session_starts_reauth(hass: HomeAssistant, api) -> None:
    """API says 401 and the website no longer knows the session either."""
    api.active.side_effect = DeliverooAuthError()
    api.account.side_effect = DeliverooAuthError()
    entry = MockConfigEntry(domain=DOMAIN, unique_id="it_123", data={CONF_MARKET: "it", CONF_TOKEN: "OLD"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1 and flows[0]["context"]["source"] == "reauth"

    api.active.side_effect = None
    with patch("custom_components.deliveroo.async_setup_entry", return_value=True):
        # a cookie from a different account is refused
        api.user.return_value = DeliverooUser("999", "Other")
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], {CONF_TOKEN: make_token(999)}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "wrong_account"
    assert entry.data[CONF_TOKEN] == "OLD"


async def test_reauth_with_fresh_cookie(hass: HomeAssistant, api) -> None:
    api.active.side_effect = DeliverooAuthError()
    api.account.side_effect = DeliverooAuthError()
    entry = MockConfigEntry(domain=DOMAIN, unique_id="it_123", data={CONF_MARKET: "it", CONF_TOKEN: "OLD"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()

    with patch("custom_components.deliveroo.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], {CONF_TOKEN: TOKEN}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == TOKEN


# ── Idle polling: API only ───────────────────────────────────────────────────


async def test_normal_operation_never_reads_the_website(hass: HomeAssistant, api) -> None:
    """Start-up and idle ticks are tiny API calls with the session token."""
    api.active.return_value = []
    entry = await setup_entry(hass)
    assert entry.runtime_data.update_interval == IDLE_INTERVAL == timedelta(seconds=30)
    assert hass.states.get("sensor.deliveroo_order_status").state == "idle"

    for i in range(1, 6):
        await tick(hass, IDLE_INTERVAL * i)
    assert api.active.await_count == 6
    assert api.account.await_count == 0
    assert api.status.await_count == 0


async def test_new_order_detected_on_next_idle_tick(hass: HomeAssistant, api) -> None:
    api.active.return_value = []
    entry = await setup_entry(hass)
    assert hass.states.get("binary_sensor.deliveroo_active_order").state == "off"

    api.active.return_value = [ACTIVE_ORDER]  # order placed from the phone
    await tick(hass, IDLE_INTERVAL)
    assert hass.states.get("binary_sensor.deliveroo_active_order").state == "on"
    assert hass.states.get("sensor.deliveroo_order_status").state == "processing"
    assert entry.runtime_data.update_interval == ACTIVE_INTERVAL
    assert api.account.await_count == 0


async def test_keep_alive_visits_the_website_every_few_hours(hass: HomeAssistant, api, freezer) -> None:
    api.active.return_value = []
    entry = await setup_entry(hass)

    freezer.tick(IDLE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert api.account.await_count == 0

    freezer.tick(SESSION_REFRESH)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert api.account.await_count == 1

    # A failing keep-alive never breaks the integration, and is not retried every tick
    api.account.side_effect = DeliverooBlockedError("HTTP 403 on orders page")
    freezer.tick(SESSION_REFRESH)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    freezer.tick(IDLE_INTERVAL)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert api.account.await_count == 2
    assert entry.runtime_data.last_update_success


async def test_keep_alive_persists_a_rotated_token(hass: HomeAssistant, api, freezer) -> None:
    api.active.return_value = []
    entry = await setup_entry(hass)
    with patch(f"{CLIENT}.async_get_account", rotate_token_on_page_read("NEW", active=False)):
        freezer.tick(SESSION_REFRESH)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert entry.data[CONF_TOKEN] == "NEW"


async def test_api_401_recovers_with_rotated_token(hass: HomeAssistant, api) -> None:
    """The API rejects the token, the website hands out a new one."""
    calls = {"n": 0}

    async def first_401_then_ok(bearer=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeliverooAuthError()
        return []

    api.active.side_effect = first_401_then_ok
    with patch(f"{CLIENT}.async_get_account", rotate_token_on_page_read("NEW", active=False)):
        entry = await setup_entry(hass)
    assert entry.runtime_data.last_update_success
    assert entry.data[CONF_TOKEN] == "NEW"
    assert not hass.config_entries.flow.async_progress()


async def test_api_401_with_same_token_asks_for_reauth(hass: HomeAssistant, api) -> None:
    """Website still logged in but hands back the same rejected token."""
    api.active.side_effect = DeliverooAuthError()
    entry = MockConfigEntry(domain=DOMAIN, unique_id="it_123", data={CONF_MARKET: "it", CONF_TOKEN: "OLD"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1 and flows[0]["context"]["source"] == "reauth"


async def test_light_api_unavailable_falls_back_to_page(hass: HomeAssistant, api) -> None:
    """Other markets may not have the endpoint: detection must still work."""
    api.active.side_effect = DeliverooError("HTTP 404 on active orders")
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    assert coordinator.last_update_success
    assert not coordinator.light_api_enabled
    # the page (which lists order 222 as active) was used instead
    assert api.account.await_count == 1
    assert hass.states.get("binary_sensor.deliveroo_active_order").state == "on"

    # once idle again, the page is never polled faster than the fallback interval
    api.status.return_value = status(is_completed=True)
    await tick(hass, ACTIVE_INTERVAL)
    assert coordinator.update_interval == FALLBACK_IDLE_INTERVAL


async def test_api_network_error_is_a_failed_update(hass: HomeAssistant, api) -> None:
    api.active.return_value = []
    entry = await setup_entry(hass)
    api.active.side_effect = DeliverooConnectionError("timeout")
    await tick(hass, IDLE_INTERVAL)
    assert not entry.runtime_data.last_update_success
    assert entry.runtime_data.light_api_enabled  # a network blip is not a fallback
    assert api.account.await_count == 0


# ── Order lifecycle ──────────────────────────────────────────────────────────


async def test_order_lifecycle(hass: HomeAssistant, api) -> None:
    events = async_capture_events(hass, EVENT_ORDER_UPDATE)
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data

    assert coordinator.update_interval == ACTIVE_INTERVAL
    assert hass.states.get("binary_sensor.deliveroo_active_order").state == "on"
    st = hass.states.get("sensor.deliveroo_order_status")
    assert st.state == "processing"
    assert st.attributes["order_id"] == "222"
    assert hass.states.get("sensor.deliveroo_estimated_arrival").state == "20:20–20:50"
    assert hass.states.get("sensor.deliveroo_rider_code").state == "78"
    assert len(events) == 1

    # Same status again: no new event, and no order-list call while active
    listed = api.active.await_count
    await tick(hass, ACTIVE_INTERVAL)
    assert len(events) == 1
    assert api.active.await_count == listed

    # Rider on the way
    api.status.return_value = status(message="Il tuo ordine è stato ritirato", rider_route="TO_CUSTOMER", progress=70)
    await tick(hass, ACTIVE_INTERVAL * 2)
    assert len(events) == 2
    assert events[-1].data["rider_route"] == "TO_CUSTOMER"

    # Delivered: order finished, back to idle polling
    api.status.return_value = status(message="Consegnato", is_completed=True)
    await tick(hass, ACTIVE_INTERVAL * 3)
    assert events[-1].data["is_completed"] is True
    assert coordinator.update_interval == IDLE_INTERVAL
    assert hass.states.get("binary_sensor.deliveroo_active_order").state == "off"
    assert hass.states.get("sensor.deliveroo_order_status").state == "completed"

    # Next idle tick: the API may still list the order for a while, must not re-poll it
    calls = api.status.await_count
    await tick(hass, ACTIVE_INTERVAL * 3 + IDLE_INTERVAL)
    assert api.status.await_count == calls
    assert hass.states.get("sensor.deliveroo_order_status").state == "idle"
    assert api.account.await_count == 0


async def test_status_401_falls_back_to_sharing_token(hass: HomeAssistant, api) -> None:
    entry = await setup_entry(hass)

    async def by_kind(order_id, *, bearer=None, sharing_token=None):
        if sharing_token is None:
            raise DeliverooAuthError()
        assert sharing_token == "SHARE"
        return status(message="via sharing token")

    api.status.side_effect = by_kind
    await tick(hass, ACTIVE_INTERVAL)
    assert entry.runtime_data.last_update_success
    assert hass.states.get("sensor.deliveroo_status_message").state == "via sharing token"
    assert api.account.await_count == 0


async def test_status_401_without_sharing_token_recovers_token(hass: HomeAssistant, api) -> None:
    calls = {"n": 0}

    async def first_401_then_ok(order_id, *, bearer=None, sharing_token=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeliverooAuthError()
        return status(sharing_token=None)

    api.status.side_effect = first_401_then_ok
    with patch(f"{CLIENT}.async_get_account", rotate_token_on_page_read("NEW")):
        entry = await setup_entry(hass)
    assert entry.runtime_data.last_update_success
    assert entry.data[CONF_TOKEN] == "NEW"
    assert hass.states.get("sensor.deliveroo_order_status").state == "processing"


# ── Sensors, options, button, diagnostics ────────────────────────────────────

STEPS = [
    DeliverooStep("Ordine inviato", 10, False),
    DeliverooStep("Attesa conferma", 30, False),
    DeliverooStep("In preparazione", 70, False),
    DeliverooStep("In transito", 90, True),
    DeliverooStep("Il tuo rider è vicino!", 100, False),
]


async def test_step_delivery_time_and_advisory(hass: HomeAssistant, api) -> None:
    """Values modelled on a real order with the rider on the way."""
    api.status.return_value = status(
        message="Il tuo ordine è stato ritirato",
        rider_route="TO_CUSTOMER",
        progress=70,
        steps=STEPS,
        estimated_delivery=datetime(2026, 10, 4, 18, 3, 18, tzinfo=UTC),
        advisory="Il rider ha un altro ordine da consegnare.",
        rider_status="en-route",
    )
    events = async_capture_events(hass, EVENT_ORDER_UPDATE)
    await setup_entry(hass)

    step = hass.states.get("sensor.deliveroo_step")
    assert step.state == "In transito"
    assert step.attributes["step_index"] == 4
    assert step.attributes["step_count"] == 5
    assert step.attributes["steps"][2] == "In preparazione"
    assert hass.states.get("sensor.deliveroo_estimated_delivery_time").state == "2026-10-04T18:03:18+00:00"
    assert hass.states.get("sensor.deliveroo_status_message").attributes["advisory"].startswith("Il rider")
    # ui_status stays PROCESSING while in transit: the step is what tells phases apart
    assert hass.states.get("sensor.deliveroo_order_status").state == "processing"
    assert events[0].data["step"] == "In transito"
    assert events[0].data["step_index"] == 4
    assert events[0].data["estimated_delivery"] == "2026-10-04T18:03:18+00:00"


async def test_options_flow_changes_intervals(hass: HomeAssistant, api) -> None:
    api.active.return_value = []
    entry = await setup_entry(hass)
    assert entry.runtime_data.update_interval == timedelta(seconds=30)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_IDLE_INTERVAL: 300, CONF_ACTIVE_INTERVAL: 15}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_IDLE_INTERVAL: 300, CONF_ACTIVE_INTERVAL: 15}
    assert entry.runtime_data.update_interval == timedelta(seconds=300)
    assert entry.runtime_data.active_interval == timedelta(seconds=15)


async def test_refresh_button_detects_new_order_immediately(hass: HomeAssistant, api) -> None:
    api.active.return_value = []
    await setup_entry(hass)
    assert hass.states.get("sensor.deliveroo_order_status").state == "idle"

    api.active.return_value = [ACTIVE_ORDER]  # an order was just placed
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.deliveroo_refresh_now"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.deliveroo_order_status").state == "processing"
    assert hass.states.get("binary_sensor.deliveroo_active_order").state == "on"
    assert api.account.await_count == 0


async def test_diagnostics_redacts_third_party_data(hass: HomeAssistant, api) -> None:
    from custom_components.deliveroo.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    raw = {
        "advisory": "Mario Rossi ha un altro ordine da consegnare",
        "rider_validation_code_s": "78",
        "partner_phone_number": "+390000000",
        "message": "Il tuo ordine è stato ritirato",
        "analytics": {"delivery_drn_id": "abc", "rider_status": "en-route"},
    }
    api.status.return_value = status(steps=STEPS, raw_attributes=raw, advisory=raw["advisory"])
    entry = await setup_entry(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dumped = str(diag)
    assert "Mario Rossi" not in dumped
    assert "+390000000" not in dumped
    assert "abc" not in dumped.replace("active", "")
    assert diag["entry"][CONF_TOKEN] == "**REDACTED**"
    assert diag["light_api"] is True
    assert diag["status"]["step_title"] == "In transito"
    assert diag["status"]["raw_attributes"]["message"] == "Il tuo ordine è stato ritirato"
