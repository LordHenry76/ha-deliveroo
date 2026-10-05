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
DEFAULT_IDLE_INTERVAL: Final = 120
DEFAULT_ACTIVE_INTERVAL: Final = 20
MIN_IDLE_INTERVAL: Final = 60
MAX_IDLE_INTERVAL: Final = 900
MIN_ACTIVE_INTERVAL: Final = 10
MAX_ACTIVE_INTERVAL: Final = 120
# While an order is active, the orders page is re-read at most this often.
ACCOUNT_REFRESH: Final = timedelta(minutes=5)

EVENT_ORDER_UPDATE: Final = f"{DOMAIN}_order_update"
