"""Home Assistant level tests: config flow, reauth, coordinator lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

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
    DeliverooOrder,
    DeliverooOrderStatus,
    DeliverooStep,
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
)

CLIENT = "custom_components.deliveroo.api.DeliverooClient"
ACTIVE_INTERVAL = timedelta(seconds=DEFAULT_ACTIVE_INTERVAL)
IDLE_INTERVAL = timedelta(seconds=DEFAULT_IDLE_INTERVAL)


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


async def test_user_flow_success(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with (
        patch(f"{CLIENT}.async_get_account", AsyncMock(return_value=account())),
        patch("custom_components.deliveroo.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MARKET: "it", CONF_TOKEN: "COOKIE"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Deliveroo (Alex)"
    assert result["data"] == {CONF_MARKET: "it", CONF_TOKEN: "COOKIE"}
    assert result["result"].unique_id == "it_123"


async def test_user_flow_errors(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    for exc, err in ((DeliverooAuthError(), "invalid_auth"), (DeliverooBlockedError(), "blocked")):
        with patch(f"{CLIENT}.async_get_account", AsyncMock(side_effect=exc)):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_MARKET: "it", CONF_TOKEN: "BAD"}
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": err}


async def test_order_lifecycle(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="it_123", title="Deliveroo (Alex)",
        data={CONF_MARKET: "it", CONF_TOKEN: "COOKIE"},
    )
    entry.add_to_hass(hass)
    events = async_capture_events(hass, EVENT_ORDER_UPDATE)

    get_account = AsyncMock(return_value=account())
    get_status = AsyncMock(return_value=status())
    with (
        patch(f"{CLIENT}.async_get_account", get_account),
        patch(f"{CLIENT}.async_get_order_status", get_status),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        assert coordinator.update_interval == ACTIVE_INTERVAL
        assert hass.states.get("binary_sensor.deliveroo_alex_active_order").state == "on"
        st = hass.states.get("sensor.deliveroo_alex_order_status")
        assert st.state == "processing"
        assert st.attributes["order_id"] == "222"
        assert hass.states.get("sensor.deliveroo_alex_estimated_arrival").state == "20:20–20:50"
        assert hass.states.get("sensor.deliveroo_alex_rider_code").state == "78"
        assert len(events) == 1

        # Same status again: no new event
        async_fire_time_changed(hass, dt_util.utcnow() + ACTIVE_INTERVAL)
        await hass.async_block_till_done()
        assert len(events) == 1

        # Rider on the way
        get_status.return_value = status(ui_status="IN_TRANSIT", message="Il rider sta arrivando",
                                         rider_route="TO_CUSTOMER", progress=80)
        async_fire_time_changed(hass, dt_util.utcnow() + ACTIVE_INTERVAL * 2)
        await hass.async_block_till_done()
        assert len(events) == 2
        assert events[-1].data["rider_route"] == "TO_CUSTOMER"

        # Delivered: order finished, back to slow polling
        get_status.return_value = status(ui_status="COMPLETED", message="Consegnato", is_completed=True)
        async_fire_time_changed(hass, dt_util.utcnow() + ACTIVE_INTERVAL * 3)
        await hass.async_block_till_done()
        assert events[-1].data["is_completed"] is True
        assert coordinator.update_interval == IDLE_INTERVAL
        assert hass.states.get("binary_sensor.deliveroo_alex_active_order").state == "off"
        assert hass.states.get("sensor.deliveroo_alex_order_status").state == "completed"

        # Next idle tick: history still says PROCESSING for a while, must not re-poll it
        calls = get_status.await_count
        async_fire_time_changed(hass, dt_util.utcnow() + ACTIVE_INTERVAL * 3 + IDLE_INTERVAL)
        await hass.async_block_till_done()
        assert get_status.await_count == calls
        assert hass.states.get("sensor.deliveroo_alex_order_status").state == "idle"


async def test_rotated_cookie_is_persisted(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="it_123", data={CONF_MARKET: "it", CONF_TOKEN: "OLD"})
    entry.add_to_hass(hass)

    async def fake_get_account(self):
        self._token = "NEW"
        return account(active=False)

    with patch(f"{CLIENT}.async_get_account", fake_get_account):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.data[CONF_TOKEN] == "NEW"


async def test_expired_session_starts_reauth(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="it_123", data={CONF_MARKET: "it", CONF_TOKEN: "OLD"})
    entry.add_to_hass(hass)

    with patch(f"{CLIENT}.async_get_account", AsyncMock(side_effect=DeliverooAuthError())):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1 and flows[0]["context"]["source"] == "reauth"

    with (
        patch(f"{CLIENT}.async_get_account", AsyncMock(return_value=account(active=False))),
        patch("custom_components.deliveroo.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], {CONF_TOKEN: "FRESH"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "FRESH"


async def _setup_active(hass: HomeAssistant, get_account, get_status) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="it_123", title="Deliveroo", data={CONF_MARKET: "it", CONF_TOKEN: "C"})
    entry.add_to_hass(hass)
    with (
        patch(f"{CLIENT}.async_get_account", get_account),
        patch(f"{CLIENT}.async_get_order_status", get_status),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_expired_bearer_does_not_refetch_page_every_minute(hass: HomeAssistant) -> None:
    """Deliveroo keeps serving an expired Bearer that the API still accepts."""
    expired = account()
    expired.bearer_expires = dt_util.utcnow().timestamp() - 600
    get_account = AsyncMock(return_value=expired)
    get_status = AsyncMock(return_value=status())
    with (
        patch(f"{CLIENT}.async_get_account", get_account),
        patch(f"{CLIENT}.async_get_order_status", get_status),
    ):
        await _setup_active(hass, get_account, get_status)
        for i in range(1, 4):
            async_fire_time_changed(hass, dt_util.utcnow() + ACTIVE_INTERVAL * i)
            await hass.async_block_till_done()
    assert get_account.await_count == 1
    assert get_status.await_count == 4


async def test_401_falls_back_to_sharing_token(hass: HomeAssistant) -> None:
    get_account = AsyncMock(return_value=account())
    get_status = AsyncMock(return_value=status())
    with (
        patch(f"{CLIENT}.async_get_account", get_account),
        patch(f"{CLIENT}.async_get_order_status", get_status),
    ):
        entry = await _setup_active(hass, get_account, get_status)

        async def by_kind(order_id, *, bearer=None, sharing_token=None):
            if bearer:
                raise DeliverooAuthError()
            assert sharing_token == "SHARE"
            return status(message="via sharing token")

        get_status.side_effect = by_kind
        async_fire_time_changed(hass, dt_util.utcnow() + ACTIVE_INTERVAL)
        await hass.async_block_till_done()
    assert entry.runtime_data.last_update_success
    assert hass.states.get("sensor.deliveroo_status_message").state == "via sharing token"


async def test_401_without_sharing_token_retries_with_fresh_bearer(hass: HomeAssistant) -> None:
    fresh = account()
    fresh.bearer = "eyJ.eyJfresh.sig"
    get_account = AsyncMock(side_effect=[account(), fresh])

    async def by_bearer(order_id, *, bearer=None, sharing_token=None):
        if bearer != fresh.bearer:
            raise DeliverooAuthError()
        return status(sharing_token=None)

    get_status = AsyncMock(side_effect=by_bearer)
    entry = await _setup_active(hass, get_account, get_status)
    assert entry.runtime_data.last_update_success
    assert get_account.await_count == 2
    assert hass.states.get("sensor.deliveroo_order_status").state == "processing"


STEPS = [
    DeliverooStep("Ordine inviato", 10, False),
    DeliverooStep("Attesa conferma", 30, False),
    DeliverooStep("In preparazione", 70, False),
    DeliverooStep("In transito", 90, True),
    DeliverooStep("Il tuo rider è vicino!", 100, False),
]


async def test_step_delivery_time_and_advisory(hass: HomeAssistant) -> None:
    """Values modelled on a real order with the rider on the way."""
    eta = datetime(2026, 10, 4, 18, 3, 18, tzinfo=UTC)
    get_account = AsyncMock(return_value=account())
    get_status = AsyncMock(
        return_value=status(
            message="Il tuo ordine è stato ritirato",
            rider_route="TO_CUSTOMER",
            progress=70,
            steps=STEPS,
            estimated_delivery=eta,
            advisory="Il rider ha un altro ordine da consegnare.",
            rider_status="en-route",
        )
    )
    events = async_capture_events(hass, EVENT_ORDER_UPDATE)
    await _setup_active(hass, get_account, get_status)

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


async def test_default_and_custom_intervals(hass: HomeAssistant) -> None:
    get_account = AsyncMock(return_value=account(active=False))
    get_status = AsyncMock(return_value=status())
    with (
        patch(f"{CLIENT}.async_get_account", get_account),
        patch(f"{CLIENT}.async_get_order_status", get_status),
    ):
        entry = await _setup_active(hass, get_account, get_status)
        assert entry.runtime_data.update_interval == timedelta(seconds=120)

        # Options flow changes the intervals and reloads the entry
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


async def test_refresh_button_detects_new_order_immediately(hass: HomeAssistant) -> None:
    get_account = AsyncMock(return_value=account(active=False))
    get_status = AsyncMock(return_value=status())
    with (
        patch(f"{CLIENT}.async_get_account", get_account),
        patch(f"{CLIENT}.async_get_order_status", get_status),
    ):
        await _setup_active(hass, get_account, get_status)
        assert hass.states.get("sensor.deliveroo_order_status").state == "idle"

        get_account.return_value = account()  # an order was just placed
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.deliveroo_refresh_now"}, blocking=True
        )
        await hass.async_block_till_done()
    assert hass.states.get("sensor.deliveroo_order_status").state == "processing"
    assert hass.states.get("binary_sensor.deliveroo_active_order").state == "on"


async def test_diagnostics_redacts_third_party_data(hass: HomeAssistant) -> None:
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
    get_account = AsyncMock(return_value=account())
    get_status = AsyncMock(return_value=status(steps=STEPS, raw_attributes=raw, advisory=raw["advisory"]))
    entry = await _setup_active(hass, get_account, get_status)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    dumped = str(diag)
    assert "Mario Rossi" not in dumped
    assert "+390000000" not in dumped
    assert "abc" not in dumped.replace("active", "")
    assert diag["entry"][CONF_TOKEN] == "**REDACTED**"
    assert diag["status"]["step_title"] == "In transito"
    assert diag["status"]["raw_attributes"]["message"] == "Il tuo ordine è stato ritirato"
