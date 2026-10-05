"""The Deliveroo integration (unofficial order tracking)."""

from __future__ import annotations

import aiohttp
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import DeliverooClient
from .const import CONF_MARKET, CONF_TOKEN
from .coordinator import DeliverooConfigEntry, DeliverooCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]


def create_client(
    hass: HomeAssistant, market: str, token: str, *, auto_cleanup: bool = True
) -> tuple[DeliverooClient, aiohttp.ClientSession]:
    """Create a client with a private, cookie-less HTTP session.

    A dedicated session with a dummy cookie jar keeps each account's cookies
    isolated from the rest of Home Assistant; the session cookie is sent
    explicitly by the client. When created during entry setup, Home Assistant
    detaches the session automatically on unload. With auto_cleanup=False the
    caller must call ``session.detach()`` (never ``close()``).
    """
    session = async_create_clientsession(
        hass, auto_cleanup=auto_cleanup, cookie_jar=aiohttp.DummyCookieJar()
    )
    client = DeliverooClient(session, market, token, str(hass.config.time_zone))
    return client, session


async def async_setup_entry(hass: HomeAssistant, entry: DeliverooConfigEntry) -> bool:
    """Set up Deliveroo from a config entry."""
    client, _ = create_client(hass, entry.data[CONF_MARKET], entry.data[CONF_TOKEN])

    coordinator = DeliverooCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DeliverooConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
