"""Constants for the Deliveroo integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "deliveroo"

CONF_MARKET: Final = "market"
CONF_TOKEN: Final = "consumer_auth_token"

# Polling: slow while idle, fast while an order is being prepared/delivered.
# Both are user-configurable (options flow), in seconds.
CONF_IDLE_INTERVAL: Final = "idle_interval"
CONF_ACTIVE_INTERVAL: Final = "active_interval"
DEFAULT_IDLE_INTERVAL: Final = 30
DEFAULT_ACTIVE_INTERVAL: Final = 20
MIN_IDLE_INTERVAL: Final = 15
MAX_IDLE_INTERVAL: Final = 900
MIN_ACTIVE_INTERVAL: Final = 10
MAX_ACTIVE_INTERVAL: Final = 120
# The (heavy) orders page is only re-read this often, to validate the session and
# pick up a rotated cookie. Idle checks use the tiny "active orders" API instead.
SESSION_REFRESH: Final = timedelta(hours=6)
# If the active-orders API is unavailable, fall back to the orders page: never
# poll it faster than this, and retry the API after LIGHT_API_RETRY.
FALLBACK_IDLE_INTERVAL: Final = timedelta(seconds=120)
LIGHT_API_RETRY: Final = timedelta(hours=1)

# Demo order (button "Simulate order"): total length and refresh rate.
SIMULATION_DURATION: Final = timedelta(seconds=150)
SIMULATION_INTERVAL: Final = timedelta(seconds=5)

EVENT_ORDER_UPDATE: Final = f"{DOMAIN}_order_update"
