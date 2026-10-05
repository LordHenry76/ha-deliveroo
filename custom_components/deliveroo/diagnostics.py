"""Diagnostics for Deliveroo (helps map unknown order states)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN
from .coordinator import DeliverooConfigEntry

TO_REDACT = {
    CONF_TOKEN,
    "sharing_token",
    "share_message",
    "partner_phone_number",
    "authenticated_via",
    "status_animation_url",
    "customer_facing_link",
    "rider_validation_code",
    "rider_validation_code_s",
    # May contain the rider's name: third-party personal data.
    "advisory",
    "delivery_drn_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DeliverooConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data.data
    status = data.status if data else None
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "active_order_id": data.active_order_id if data else None,
        "light_api": entry.runtime_data.light_api_enabled,
        "status": None
        if status is None
        else {
            "state": status.state,
            "step_index": status.step_index,
            "step_title": status.step_title,
            "rider_status": status.rider_status,
            "estimated_delivery": status.estimated_delivery.isoformat()
            if status.estimated_delivery
            else None,
            "ui_status": status.ui_status,
            "eta_status": status.eta_status,
            "rider_route": status.rider_route,
            "progress": status.progress,
            "is_completed": status.is_completed,
            "is_failed": status.is_failed,
            "raw_attributes": async_redact_data(status.raw_attributes, TO_REDACT),
        },
    }
