"""Demo order: replay a full delivery without contacting Deliveroo.

The payload has the same shape as a real ``consumer_order_statuses`` document
(modelled on a real order), so it goes through the normal parser and drives
the same sensors and events. This module must not import Home Assistant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

DEMO_ORDER_ID = "demo"

# Progress percentage at which each of Deliveroo's five steps ends.
STEP_ENDS = (10, 30, 70, 90, 100)

# Demo route, as (lat, lon) offsets from the customer: about 2.5 km by road.
_ROUTE_OFFSETS = ((0.012, -0.016), (0.012, -0.004), (0.003, -0.004), (0.003, 0.0), (0.0, 0.0))
_ROUTE_MINUTES = 12
_RIDER_ICON = (
    "https://order-status-public-assets.roocdn.com/images/rider_icons/default/"
    "scooter.png?width={w}&fit=contain"
)

_TEXT: dict[str, dict[str, Any]] = {
    "it": {
        "restaurant": "Trattoria Demo",
        "steps": (
            "Ordine inviato",
            "Attesa conferma",
            "In preparazione",
            "In transito",
            "Il tuo rider è vicino!",
        ),
        "messages": (
            "Abbiamo inviato il tuo ordine a Trattoria Demo",
            "Trattoria Demo sta confermando il tuo ordine",
            "Trattoria Demo sta preparando il tuo ordine",
            "Il tuo ordine è stato ritirato",
            "Il rider sta arrivando da te",
        ),
        "advisory": "Il rider ha un altro ordine da consegnare lungo il tragitto e sarà presto da te.",
        "done": "Il tuo ordine è arrivato. Buon appetito!",
        "eta_status": "In orario",
    },
    "fr": {
        "restaurant": "Restaurant Démo",
        "steps": (
            "Confirmation en cours",
            "Acceptation en cours",
            "En cours de préparation",
            "En cours de livraison",
            "À proximité",
        ),
        "messages": (
            "Merci pour votre commande ! Restaurant Démo va la confirmer",
            "Restaurant Démo accepte votre commande",
            "Restaurant Démo prépare votre commande",
            "Votre commande est en route",
            "Votre livreur est à proximité",
        ),
        "advisory": "Votre livreur a une autre commande à livrer en chemin et sera bientôt chez vous.",
        "done": "Votre commande est arrivée. Bon appétit !",
        "eta_status": "À l'heure",
    },
    "en": {
        "restaurant": "Demo Kitchen",
        "steps": (
            "Order sent",
            "Awaiting confirmation",
            "Preparing",
            "In transit",
            "Your rider is nearby!",
        ),
        "messages": (
            "We've sent your order to Demo Kitchen",
            "Demo Kitchen is confirming your order",
            "Demo Kitchen is preparing your order",
            "Your order has been picked up",
            "Your rider is arriving",
        ),
        "advisory": "Your rider has another order to deliver on the way and will be with you soon.",
        "done": "Your order has arrived. Enjoy!",
        "eta_status": "On time",
    },
}


def encode_polyline(points: list[tuple[float, float]]) -> str:
    """Encode (lat, lon) points as a Google polyline (precision 1e5)."""
    chunks: list[str] = []
    previous = (0, 0)
    for lat, lon in points:
        current = (round(lat * 1e5), round(lon * 1e5))
        for value in (current[0] - previous[0], current[1] - previous[1]):
            value = ~(value << 1) if value < 0 else value << 1
            while value >= 0x20:
                chunks.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            chunks.append(chr(value + 63))
        previous = current
    return "".join(chunks)


def _route(home: tuple[float, float], fraction: float) -> list[tuple[float, float]]:
    """Remaining demo route once ``fraction`` of it is covered, rider first."""
    corners = [(home[0] + dlat, home[1] + dlon) for dlat, dlon in _ROUTE_OFFSETS]
    legs = list(zip(corners, corners[1:]))
    position = min(max(fraction, 0.0), 1.0) * len(legs)
    leg = min(int(position), len(legs) - 1)
    (lat1, lon1), (lat2, lon2) = legs[leg]
    t = position - leg
    rider = (lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t)
    return [rider, *(end for _, end in legs[leg:])]


def _map_entries(
    home: tuple[float, float], fraction: float, *, nearby: bool
) -> list[dict[str, Any]]:
    """Customer and rider locations plus the rider entry, as in a real order."""
    route = _route(home, fraction)
    return [
        {
            "id": "customer-demo",
            "type": "location",
            "attributes": {
                "latitude": home[0],
                "longitude": home[1],
                "type": "CUSTOMER",
                "pin_type": "star-circle",
            },
        },
        {
            "id": "rider-demo",
            "type": "location",
            "attributes": {
                "latitude": round(route[0][0], 7),
                "longitude": round(route[0][1], 7),
                "type": "RIDER",
            },
        },
        {
            "id": "demo",
            "type": "rider",
            "attributes": {
                "subtitle": "Scooter",
                "vehicle_type": "SCOOTER",
                # Deliveroo stops sending the route once the rider is nearby.
                "route_polyline_points": "" if nearby else encode_polyline(route),
                "map_icon_url": _RIDER_ICON,
            },
        },
    ]


def build_demo_payload(
    elapsed: float,
    duration: float,
    *,
    lang: str,
    start_local: datetime,
    home: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Return the status document of the demo order ``elapsed`` seconds in.

    The five steps get an equal share of ``duration``; once it is over the
    order is completed. With ``home`` (the customer), a rider drives towards
    it during the last two steps.
    """
    text = _TEXT.get(lang, _TEXT["en"])
    fraction = max(0.0, elapsed / duration) if duration > 0 else 1.0
    completed = fraction >= 1.0

    position = min(fraction, 0.999999) * len(STEP_ENDS)
    step = int(position)
    step_start = STEP_ENDS[step - 1] if step else 0
    progress = round(step_start + (position - step) * (STEP_ENDS[step] - step_start))

    delivery = start_local + timedelta(seconds=duration)
    window_end = delivery + timedelta(minutes=10)

    attributes: dict[str, Any] = {
        "ui_status": "COMPLETED" if completed else "PROCESSING",
        "message": text["done"] if completed else text["messages"][step],
        "eta_message": f"{delivery:%H:%M}–{window_end:%H:%M}",
        "eta_status": text["eta_status"],
        "eta_status_code": "ON_TIME",
        "current_progress_percentage": 100 if completed else progress,
        "rider_validation_code_s": "12",
        "is_completed": completed,
        "is_failed": False,
        "processing_steps": [
            {
                "title": title,
                "ends_at_progress_percentage": STEP_ENDS[index],
                "is_current": not completed and index == step,
            }
            for index, title in enumerate(text["steps"])
        ],
        "analytics": {
            "estimated_delivery_time": delivery.astimezone(UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        },
        "updated_at": f"{start_local + timedelta(seconds=elapsed):%Y-%m-%d %H:%M:%S %z}",
    }
    # Like the real API: no rider route or status before a rider is assigned
    # (80 % into "preparing" on the order this is modelled on) or once delivered.
    if not completed and step >= 3:
        attributes["rider_route"] = "TO_CUSTOMER"
        attributes["analytics"]["rider_status"] = "nearby" if step == 4 else "en-route"
    elif not completed and step == 2 and position - step >= 0.8:
        attributes["rider_route"] = "TO_RESTAURANT"
    if not completed and step == 3:
        attributes["advisory"] = text["advisory"]

    included: list[dict[str, Any]] = [
        {
            "type": "order",
            "attributes": {
                "order_number": "0000",
                "restaurant_name": text["restaurant"],
            },
        }
    ]
    show_map = home is not None and not completed and step >= 3
    attributes["show_map"] = show_map
    if show_map:
        # Rider covers the route over the "in transit" step, then waits nearby.
        covered = position - step if step == 3 else 0.97
        attributes["analytics"]["rider_route_duration"] = (
            0 if step == 4 else round(_ROUTE_MINUTES * (1 - covered))
        )
        included += _map_entries(home, covered, nearby=step == 4)

    return {
        "data": {"type": "consumer_order_status", "attributes": attributes},
        "included": included,
    }
