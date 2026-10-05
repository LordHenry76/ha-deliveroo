"""Data update coordinator for Deliveroo."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DeliverooAccount,
    DeliverooAuthError,
    DeliverooClient,
    DeliverooConnectionError,
    DeliverooError,
    DeliverooOrder,
    DeliverooOrderStatus,
    parse_order_status,
)
from .const import (
    CONF_ACTIVE_INTERVAL,
    CONF_IDLE_INTERVAL,
    CONF_TOKEN,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    EVENT_ORDER_UPDATE,
    FALLBACK_IDLE_INTERVAL,
    LIGHT_API_RETRY,
    SESSION_REFRESH,
    SIMULATION_DURATION,
    SIMULATION_INTERVAL,
)
from .simulation import DEMO_ORDER_ID, build_demo_payload

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DeliverooData:
    """Snapshot exposed to the entities."""

    active_order_id: str | None
    status: DeliverooOrderStatus | None


type DeliverooConfigEntry = ConfigEntry[DeliverooCoordinator]


class DeliverooCoordinator(DataUpdateCoordinator[DeliverooData]):
    """Polls the Deliveroo API at two speeds.

    Idle: a tiny "active orders" call. During an order: the live status
    endpoint. Both use the session token directly as a Bearer token.

    The website's orders page is not needed for normal operation. It is read:
    * every few hours as a best-effort session keep-alive,
    * when the API rejects the token (Deliveroo may have rotated it),
    * as a fallback to detect orders if the lightweight API is unavailable.
    """

    config_entry: DeliverooConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DeliverooConfigEntry,
        client: DeliverooClient,
    ) -> None:
        """Initialise the coordinator."""
        self.idle_interval = timedelta(
            seconds=entry.options.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL)
        )
        self.active_interval = timedelta(
            seconds=entry.options.get(CONF_ACTIVE_INTERVAL, DEFAULT_ACTIVE_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=self.idle_interval,
        )
        self.client = client
        self._active_id: str | None = None
        self._sharing_tokens: dict[str, str] = {}
        self._finished: set[str] = set()
        self._last_signature: tuple | None = None
        self._light_api_disabled_until: datetime | None = None
        self._last_page_read: datetime = dt_util.utcnow()
        self._simulation_start: datetime | None = None

    @property
    def light_api_enabled(self) -> bool:
        """Return True if idle checks use the lightweight active-orders API."""
        until = self._light_api_disabled_until
        return until is None or dt_util.utcnow() >= until

    @property
    def simulation_active(self) -> bool:
        """Return True while the demo order is being replayed."""
        return self._simulation_start is not None

    async def async_force_refresh(self) -> None:
        """Check for a new order now (used by the refresh button)."""
        await self.async_refresh()

    async def async_start_simulation(self) -> None:
        """Replay a full demo delivery; Deliveroo is not contacted meanwhile."""
        self._simulation_start = dt_util.utcnow()
        self._last_signature = None
        await self.async_refresh()

    def _simulate(self) -> DeliverooData:
        """Return the current frame of the demo order."""
        assert self._simulation_start is not None
        start = self._simulation_start
        payload = build_demo_payload(
            (dt_util.utcnow() - start).total_seconds(),
            SIMULATION_DURATION.total_seconds(),
            lang=self.client.language,
            start_local=dt_util.as_local(start),
        )
        status = parse_order_status(DEMO_ORDER_ID, payload)
        self._fire_event_if_changed(status, simulated=True)
        if status.is_completed:
            # Show "completed" until the next regular poll, then back to normal.
            self._simulation_start = None
            self.update_interval = self.idle_interval
            return DeliverooData(active_order_id=None, status=status)
        self.update_interval = SIMULATION_INTERVAL
        return DeliverooData(active_order_id=DEMO_ORDER_ID, status=status)

    # ── Website page: keep-alive, token recovery, fallback ──────────────────

    def _persist_token(self) -> None:
        """Save the token if Deliveroo rotated it."""
        entry = self.config_entry
        if self.client.token != entry.data.get(CONF_TOKEN):
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_TOKEN: self.client.token}
            )

    async def _async_read_page(self) -> DeliverooAccount:
        """Read the orders page and persist a rotated token."""
        account = await self.client.async_get_account()
        self._last_page_read = dt_util.utcnow()
        self._persist_token()
        return account

    async def _async_recover_token(self) -> None:
        """The API rejected the token: see if the website hands out a new one.

        Raises ConfigEntryAuthFailed (→ re-authentication) unless a different
        token was obtained.
        """
        previous = self.client.token
        try:
            await self._async_read_page()
        except DeliverooAuthError as err:
            raise ConfigEntryAuthFailed("Deliveroo session expired") from err
        except DeliverooError as err:
            raise UpdateFailed(f"Deliveroo token recovery: {err}") from err
        if self.client.token == previous:
            raise ConfigEntryAuthFailed("Deliveroo rejected the session token")

    async def _async_keep_alive(self) -> None:
        """Best effort: visit the website now and then, like a browser would."""
        if dt_util.utcnow() - self._last_page_read < SESSION_REFRESH:
            return
        self._last_page_read = dt_util.utcnow()  # do not retry on every tick
        try:
            await self._async_read_page()
        except DeliverooAuthError:
            _LOGGER.warning(
                "The Deliveroo website no longer recognises the session, "
                "but the API still accepts it"
            )
        except DeliverooError as err:
            _LOGGER.debug("Deliveroo keep-alive failed: %s", err)

    # ── Order detection ─────────────────────────────────────────────────────

    async def _async_list_active(self) -> list[DeliverooOrder]:
        """Active orders from the lightweight API, recovering the token on 401."""
        try:
            return await self.client.async_get_active_orders()
        except DeliverooAuthError:
            _LOGGER.debug("Token rejected by the active-orders API")
        await self._async_recover_token()
        try:
            return await self.client.async_get_active_orders()
        except DeliverooAuthError as err:
            raise ConfigEntryAuthFailed("Deliveroo rejected the session token") from err

    async def _async_list_active_from_page(self) -> list[DeliverooOrder]:
        try:
            return (await self._async_read_page()).active_orders
        except DeliverooAuthError as err:
            raise ConfigEntryAuthFailed("Deliveroo session expired") from err
        except DeliverooError as err:
            raise UpdateFailed(f"Deliveroo orders page: {err}") from err

    async def _async_detect_active(self) -> None:
        """Find out whether an order is in progress."""
        orders: list[DeliverooOrder]
        if self.light_api_enabled:
            try:
                orders = await self._async_list_active()
            except DeliverooConnectionError as err:
                raise UpdateFailed(f"Deliveroo active orders: {err}") from err
            except DeliverooError as err:
                # Endpoint missing in this market or rate limited: use the page.
                _LOGGER.warning(
                    "Deliveroo active-orders API unavailable (%s); "
                    "falling back to the orders page for %s",
                    err,
                    LIGHT_API_RETRY,
                )
                self._light_api_disabled_until = dt_util.utcnow() + LIGHT_API_RETRY
                orders = await self._async_list_active_from_page()
        else:
            orders = await self._async_list_active_from_page()

        active = [o for o in orders if o.id not in self._finished]
        self._active_id = active[0].id if active else None

    # ── Live status ─────────────────────────────────────────────────────────

    async def _async_fetch_status(self, order_id: str) -> DeliverooOrderStatus:
        try:
            return await self.client.async_get_order_status(order_id)
        except DeliverooAuthError:
            _LOGGER.debug("Token rejected for order %s", order_id)

        # 1) Public sharing token, if we already know it for this order:
        #    keep tracking the order, and sort the session out afterwards.
        sharing = self._sharing_tokens.get(order_id)
        if sharing is not None:
            return await self.client.async_get_order_status(
                order_id, sharing_token=sharing
            )

        # 2) See if the website hands out a new token and retry once.
        await self._async_recover_token()
        try:
            return await self.client.async_get_order_status(order_id)
        except DeliverooAuthError as err:
            raise ConfigEntryAuthFailed("Deliveroo rejected the session token") from err

    async def _async_update_data(self) -> DeliverooData:
        if self._simulation_start is not None:
            return self._simulate()

        if self._active_id is None:
            await self._async_detect_active()

        status: DeliverooOrderStatus | None = None
        if self._active_id is not None:
            order_id = self._active_id
            try:
                status = await self._async_fetch_status(order_id)
            except DeliverooError as err:
                raise UpdateFailed(f"Deliveroo order status: {err}") from err

            if status.sharing_token:
                self._sharing_tokens[order_id] = status.sharing_token
            if status.is_completed or status.is_failed:
                self._finished.add(order_id)
                self._sharing_tokens.pop(order_id, None)
                self._active_id = None

            self._fire_event_if_changed(status)

        if self.light_api_enabled:
            await self._async_keep_alive()

        if self._active_id is not None:
            self.update_interval = self.active_interval
        elif self.light_api_enabled:
            self.update_interval = self.idle_interval
        else:
            self.update_interval = max(self.idle_interval, FALLBACK_IDLE_INTERVAL)

        return DeliverooData(active_order_id=self._active_id, status=status)

    def _fire_event_if_changed(
        self, status: DeliverooOrderStatus, *, simulated: bool = False
    ) -> None:
        signature = (
            status.order_id,
            status.state,
            status.step_index,
            status.message,
            status.rider_route,
            status.eta,
            status.advisory,
        )
        if signature == self._last_signature:
            return
        self._last_signature = signature
        estimated = status.estimated_delivery
        self.hass.bus.async_fire(
            EVENT_ORDER_UPDATE,
            {
                "config_entry_id": self.config_entry.entry_id,
                "order_id": status.order_id,
                "state": status.state,
                "step": status.step_title,
                "step_index": status.step_index,
                "step_count": len(status.steps) or None,
                "message": status.message,
                "advisory": status.advisory,
                "eta": status.eta,
                "eta_status": status.eta_status,
                "estimated_delivery": estimated.isoformat() if estimated else None,
                "progress": status.progress,
                "rider_route": status.rider_route,
                "rider_status": status.rider_status,
                "rider_code": status.rider_code,
                "restaurant": status.restaurant_name,
                "is_completed": status.is_completed,
                "is_failed": status.is_failed,
                "simulated": simulated,
            },
        )
