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
    DeliverooError,
    DeliverooOrderStatus,
)
from .const import (
    ACCOUNT_REFRESH,
    CONF_ACTIVE_INTERVAL,
    CONF_IDLE_INTERVAL,
    CONF_TOKEN,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    EVENT_ORDER_UPDATE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DeliverooData:
    """Snapshot exposed to the entities."""

    account_name: str | None
    active_order_id: str | None
    status: DeliverooOrderStatus | None


type DeliverooConfigEntry = ConfigEntry[DeliverooCoordinator]


class DeliverooCoordinator(DataUpdateCoordinator[DeliverooData]):
    """Two-speed poller: order history while idle, live status during an order."""

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
        self._account: DeliverooAccount | None = None
        self._account_fetched: datetime | None = None
        self._active_id: str | None = None
        self._sharing_tokens: dict[str, str] = {}
        self._finished: set[str] = set()
        self._last_signature: tuple | None = None

    async def async_force_refresh(self) -> None:
        """Re-read the order history now (used by the refresh button)."""
        self._account_fetched = None
        await self.async_refresh()

    def _account_is_stale(self) -> bool:
        now = dt_util.utcnow()
        if self._account is None or self._account_fetched is None:
            return True
        if self._active_id is None:
            # Idle: every tick is an account refresh (the interval is already slow).
            return True
        # The Bearer's "exp" is deliberately ignored: the website keeps serving the
        # same token after it expires and the API still accepts it. The token is
        # refreshed only when the API answers 401 (see _async_fetch_status).
        return now - self._account_fetched >= ACCOUNT_REFRESH

    async def _async_refresh_account(self) -> None:
        try:
            account = await self.client.async_get_account()
        except DeliverooAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except DeliverooError as err:
            raise UpdateFailed(f"Deliveroo orders page: {err}") from err

        self._account = account
        self._account_fetched = dt_util.utcnow()

        # Deliveroo may rotate the long-lived cookie: persist the new value.
        entry = self.config_entry
        if self.client.token != entry.data.get(CONF_TOKEN):
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_TOKEN: self.client.token}
            )

        active = [o for o in account.active_orders if o.id not in self._finished]
        self._active_id = active[0].id if active else None

    async def _async_fetch_status(self, order_id: str) -> DeliverooOrderStatus:
        assert self._account is not None
        try:
            return await self.client.async_get_order_status(
                order_id, bearer=self._account.bearer
            )
        except DeliverooAuthError:
            _LOGGER.debug("Bearer rejected for order %s", order_id)

        # 1) Public sharing token, if we already know it for this order.
        sharing = self._sharing_tokens.get(order_id)
        if sharing is not None:
            self._account_fetched = None  # re-read the orders page next tick
            return await self.client.async_get_order_status(
                order_id, sharing_token=sharing
            )

        # 2) Re-read the orders page now for a fresh Bearer and retry once.
        previous = self._account.bearer
        await self._async_refresh_account()
        if self._account.bearer == previous:
            raise DeliverooAuthError("Bearer rejected and no fresh token available")
        return await self.client.async_get_order_status(
            order_id, bearer=self._account.bearer
        )

    async def _async_update_data(self) -> DeliverooData:
        if self._account_is_stale():
            await self._async_refresh_account()
        assert self._account is not None

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

        self.update_interval = (
            self.active_interval if self._active_id else self.idle_interval
        )

        return DeliverooData(
            account_name=self._account.name,
            active_order_id=self._active_id,
            status=status,
        )

    def _fire_event_if_changed(self, status: DeliverooOrderStatus) -> None:
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
            },
        )
