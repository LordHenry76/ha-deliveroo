"""Unofficial client for the Deliveroo consumer web API.

Deliveroo has no public consumer API. This module relies on what the
deliveroo.<tld> website itself uses:

* ``GET https://deliveroo.<tld>/<lang>/orders`` rendered server side (Next.js).
  With the long-lived ``consumer_auth_token`` cookie it returns the order
  history and a short-lived Bearer JWT embedded in ``__NEXT_DATA__``.
* ``GET https://api.<market>.deliveroo.com/consumer/order-history/v1/orders?state=active``
  which returns only the orders in progress (a few bytes when there are none),
  authenticated with the Bearer JWT. This is what makes frequent idle checks cheap.
* ``GET https://api.<market>.deliveroo.com/consumer/v2-6/consumer_order_statuses/<id>``
  which returns the live tracking data (JSON:API), authenticated either with the
  Bearer JWT or with the order's public ``sharing_token``.

This module must not import Home Assistant so it can be tested standalone.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

COOKIE_NAME = "consumer_auth_token"
JWT_ISSUER = "rooconsumerauth"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
STATUS_PATH = "/consumer/v2-6/consumer_order_statuses/{order_id}"
ORDER_HISTORY_PATH = "/consumer/order-history/v1/orders"

# Only "it" has been verified end to end. The others follow the same pattern
# (web domain + api.<market>.deliveroo.com) and are considered experimental.
MARKETS: dict[str, dict[str, str]] = {
    "it": {"web": "https://deliveroo.it", "api": "https://api.it.deliveroo.com", "lang": "it"},
    "uk": {"web": "https://deliveroo.co.uk", "api": "https://api.uk.deliveroo.com", "lang": "en"},
    "ie": {"web": "https://deliveroo.ie", "api": "https://api.ie.deliveroo.com", "lang": "en"},
    "fr": {"web": "https://deliveroo.fr", "api": "https://api.fr.deliveroo.com", "lang": "fr"},
    "be": {"web": "https://deliveroo.be", "api": "https://api.be.deliveroo.com", "lang": "fr"},
}

# consumerStatusCode / status values that mean the order is over.
# "COMPLETE" / "DELIVERED" have been observed; the rest are defensive guesses.
TERMINAL_CONSUMER_CODES = {"COMPLETE", "COMPLETED", "FAILED", "CANCELLED", "CANCELED"}
TERMINAL_STATUSES = {
    "DELIVERED",
    "CANCELLED",
    "CANCELED",
    "REJECTED",
    "FAILED",
    "REFUNDED",
    "UNDELIVERABLE",
}

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.(eyJ[A-Za-z0-9_-]+)\.[A-Za-z0-9_-]+")


class DeliverooError(Exception):
    """Generic Deliveroo error."""


class DeliverooConnectionError(DeliverooError):
    """Network problem talking to Deliveroo."""


class DeliverooAuthError(DeliverooError):
    """The session cookie or Bearer token is invalid or expired."""


class DeliverooBlockedError(DeliverooError):
    """The request was rejected by Deliveroo's bot protection."""


def decode_jwt_payload(segment: str) -> dict[str, Any]:
    """Decode the (unverified) payload segment of a JWT."""
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


@dataclass(slots=True)
class DeliverooOrder:
    """An order from the order history."""

    id: str
    status: str | None
    consumer_status_code: str | None
    status_text: str | None
    restaurant_name: str | None

    @property
    def is_active(self) -> bool:
        """Return True if the order is still in progress."""
        if (self.consumer_status_code or "").upper() in TERMINAL_CONSUMER_CODES:
            return False
        if (self.status or "").upper() in TERMINAL_STATUSES:
            return False
        return True


@dataclass(slots=True)
class DeliverooAccount:
    """Data extracted from the server-rendered orders page."""

    customer_id: str
    name: str | None
    bearer: str
    bearer_expires: float
    orders: list[DeliverooOrder] = field(default_factory=list)

    @property
    def active_orders(self) -> list[DeliverooOrder]:
        """Active orders, newest first (the page already sorts them)."""
        return [order for order in self.orders if order.is_active]


@dataclass(slots=True)
class DeliverooStep:
    """One step of the order timeline provided by Deliveroo."""

    title: str
    ends_at_progress: int | None
    is_current: bool


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp such as 2026-10-04T18:03:18Z."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


@dataclass(slots=True)
class DeliverooOrderStatus:
    """Live tracking data for one order."""

    order_id: str
    ui_status: str | None
    message: str | None
    eta: str | None
    eta_status: str | None
    progress: int | None
    rider_route: str | None
    rider_code: str | None
    is_completed: bool
    is_failed: bool
    restaurant_name: str | None
    order_number: str | None
    sharing_token: str | None
    updated_at: str | None
    advisory: str | None = None
    rider_status: str | None = None
    estimated_delivery: datetime | None = None
    steps: list[DeliverooStep] = field(default_factory=list)
    raw_attributes: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def step_index(self) -> int | None:
        """1-based position of the current step in Deliveroo's own timeline."""
        for index, step in enumerate(self.steps, start=1):
            if step.is_current:
                return index
        return None

    @property
    def step_title(self) -> str | None:
        """Localised title of the current step, as shown by Deliveroo."""
        index = self.step_index
        return self.steps[index - 1].title if index else None

    @property
    def state(self) -> str:
        """Normalised state used by the status sensor."""
        if self.is_failed:
            return "failed"
        if self.is_completed:
            return "completed"
        return (self.ui_status or "unknown").lower()


def parse_orders_page(html: str) -> DeliverooAccount:
    """Parse the server-rendered /<lang>/orders page."""
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        raise DeliverooBlockedError("Orders page has no __NEXT_DATA__ (blocked?)")

    raw = match.group(1)
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise DeliverooError("Invalid __NEXT_DATA__ JSON") from err

    state = (data.get("props") or {}).get("initialState") or {}
    user = state.get("user") or {}
    if not user.get("isLoggedIn"):
        raise DeliverooAuthError("Session cookie is not logged in")

    bearer: str | None = None
    bearer_exp = 0.0
    bearer_claims: dict[str, Any] = {}
    for jwt_match in _JWT_RE.finditer(raw):
        try:
            claims = decode_jwt_payload(jwt_match.group(1))
        except (ValueError, UnicodeDecodeError):
            continue
        if claims.get("iss") != JWT_ISSUER:
            continue
        exp = float(claims.get("exp") or 0)
        if exp > bearer_exp:
            bearer, bearer_exp, bearer_claims = jwt_match.group(0), exp, claims
    if bearer is None:
        raise DeliverooError("No consumer Bearer token found in the orders page")

    history = ((state.get("order") or {}).get("history") or {}).get("orders") or []
    orders = [
        DeliverooOrder(
            id=str(item.get("id")),
            status=item.get("status"),
            consumer_status_code=item.get("consumerStatusCode"),
            status_text=item.get("statusText"),
            restaurant_name=(item.get("restaurantName") or "").strip() or None,
        )
        for item in history
        if item.get("id") is not None
    ]

    customer_id = user.get("id") or bearer_claims.get("cust")
    if customer_id is None:
        raise DeliverooError("Customer id not found")

    return DeliverooAccount(
        customer_id=str(customer_id),
        name=user.get("preferredName") or user.get("firstName"),
        bearer=bearer,
        bearer_expires=bearer_exp,
        orders=orders,
    )


def parse_order_list(payload: Any) -> list[DeliverooOrder]:
    """Parse an order-history document ({"orders": [...], "count": n})."""
    items = payload.get("orders") if isinstance(payload, dict) else None
    orders: list[DeliverooOrder] = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        restaurant = item.get("restaurant")
        name = restaurant.get("name") if isinstance(restaurant, dict) else None
        status = item.get("status")
        consumer_status = item.get("consumer_status")
        orders.append(
            DeliverooOrder(
                id=str(item["id"]),
                status=status if isinstance(status, str) else None,
                consumer_status_code=consumer_status
                if isinstance(consumer_status, str)
                else None,
                status_text=None,
                restaurant_name=(name or "").strip() or None
                if isinstance(name, str)
                else None,
            )
        )
    return orders


def parse_order_status(order_id: str, payload: dict[str, Any]) -> DeliverooOrderStatus:
    """Parse a consumer_order_statuses JSON:API document."""
    data = payload.get("data") or {}
    attrs: dict[str, Any] = data.get("attributes") or {}

    order_attrs: dict[str, Any] = {}
    for item in payload.get("included") or []:
        if item.get("type") == "order":
            order_attrs = item.get("attributes") or {}
            break

    progress = attrs.get("current_progress_percentage")
    analytics = attrs.get("analytics") if isinstance(attrs.get("analytics"), dict) else {}
    steps = [
        DeliverooStep(
            title=str(step.get("title") or ""),
            ends_at_progress=step.get("ends_at_progress_percentage")
            if isinstance(step.get("ends_at_progress_percentage"), int)
            else None,
            is_current=bool(step.get("is_current")),
        )
        for step in attrs.get("processing_steps") or []
        if isinstance(step, dict)
    ]
    rider_code = attrs.get("rider_validation_code_s") or attrs.get("rider_validation_code")

    return DeliverooOrderStatus(
        order_id=str(order_id),
        ui_status=attrs.get("ui_status"),
        message=attrs.get("message"),
        eta=attrs.get("eta_message"),
        eta_status=attrs.get("eta_status_code"),
        progress=int(progress) if isinstance(progress, (int, float)) else None,
        rider_route=attrs.get("rider_route"),
        rider_code=str(rider_code) if rider_code not in (None, "") else None,
        is_completed=bool(attrs.get("is_completed")),
        is_failed=bool(attrs.get("is_failed")),
        restaurant_name=(order_attrs.get("restaurant_name") or "").strip() or None,
        order_number=order_attrs.get("order_number"),
        sharing_token=order_attrs.get("sharing_token"),
        updated_at=attrs.get("updated_at"),
        advisory=(attrs.get("advisory") or "").strip() or None,
        rider_status=analytics.get("rider_status"),
        estimated_delivery=_parse_datetime(analytics.get("estimated_delivery_time")),
        steps=steps,
        raw_attributes=attrs,
    )


class DeliverooClient:
    """Minimal async client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        market: str,
        token: str,
        time_zone: str,
    ) -> None:
        """Initialise the client."""
        if market not in MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self._session = session
        self._market = MARKETS[market]
        self._token = token.strip()
        self._time_zone = time_zone

    @property
    def token(self) -> str:
        """Current session cookie (Deliveroo may rotate it)."""
        return self._token

    def _base_headers(self) -> dict[str, str]:
        lang = self._market["lang"]
        return {
            "User-Agent": USER_AGENT,
            "Accept-Language": f"{lang},en;q=0.8",
        }

    async def async_get_account(self) -> DeliverooAccount:
        """Fetch the orders page: session check, Bearer token and order history."""
        url = f"{self._market['web']}/{self._market['lang']}/orders"
        headers = {
            **self._base_headers(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cookie": f"{COOKIE_NAME}={self._token}",
        }
        try:
            async with self._session.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                body = await resp.text()
                status = resp.status
                rotated = resp.cookies.get(COOKIE_NAME)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DeliverooConnectionError(str(err)) from err

        if rotated is not None and rotated.value and rotated.value != self._token:
            _LOGGER.debug("Deliveroo rotated the session cookie")
            self._token = rotated.value

        if status == 401:
            raise DeliverooAuthError("HTTP 401 on orders page")
        if status in (403, 429, 503):
            raise DeliverooBlockedError(f"HTTP {status} on orders page")
        if status != 200:
            raise DeliverooError(f"HTTP {status} on orders page")
        return parse_orders_page(body)

    async def async_get_active_orders(self, bearer: str) -> list[DeliverooOrder]:
        """Fetch only the orders in progress (tiny response when there are none)."""
        headers = {
            **self._base_headers(),
            "Accept": "application/json, application/vnd.api+json",
            "Authorization": f"Bearer {bearer}",
            "Origin": self._market["web"],
            "Referer": f"{self._market['web']}/",
            "X-Roo-RequestSource": "orders",
        }
        url = self._market["api"] + ORDER_HISTORY_PATH
        try:
            async with self._session.get(
                url,
                headers=headers,
                params={"state": "active"},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                body = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DeliverooConnectionError(str(err)) from err

        if status == 401:
            raise DeliverooAuthError("HTTP 401 on active orders")
        if status in (403, 429, 503):
            raise DeliverooBlockedError(f"HTTP {status} on active orders")
        if status != 200:
            raise DeliverooError(f"HTTP {status} on active orders")
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise DeliverooError("Invalid active orders JSON") from err
        return parse_order_list(payload)

    async def async_get_order_status(
        self,
        order_id: str,
        *,
        bearer: str | None = None,
        sharing_token: str | None = None,
    ) -> DeliverooOrderStatus:
        """Fetch live tracking data with the Bearer token or the sharing token."""
        params = {"tz": self._time_zone}
        headers = {
            **self._base_headers(),
            "Accept": "application/json, application/vnd.api+json",
            "Origin": self._market["web"],
            "Referer": f"{self._market['web']}/",
        }
        if sharing_token:
            params["sharing_token"] = sharing_token
        elif bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        else:
            raise ValueError("A bearer token or a sharing token is required")

        url = self._market["api"] + STATUS_PATH.format(order_id=order_id)
        try:
            async with self._session.get(
                url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            ) as resp:
                body = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DeliverooConnectionError(str(err)) from err

        if status == 401:
            raise DeliverooAuthError("HTTP 401 on order status")
        if status in (403, 429, 503):
            raise DeliverooBlockedError(f"HTTP {status} on order status")
        if status != 200:
            raise DeliverooError(f"HTTP {status} on order status")
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise DeliverooError("Invalid order status JSON") from err
        return parse_order_status(order_id, payload)
