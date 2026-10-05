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


def build_demo_payload(
    elapsed: float,
    duration: float,
    *,
    lang: str,
    start_local: datetime,
) -> dict[str, Any]:
    """Return the status document of the demo order ``elapsed`` seconds in.

    The five steps get an equal share of ``duration``; once it is over the
    order is completed.
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
        "rider_route": "TO_CUSTOMER" if completed or step >= 3 else "TO_RESTAURANT",
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
            "rider_status": "en-route",
        },
        "updated_at": f"{start_local + timedelta(seconds=elapsed):%Y-%m-%d %H:%M:%S %z}",
    }
    if not completed and step == 3:
        attributes["advisory"] = text["advisory"]

    return {
        "data": {"type": "consumer_order_status", "attributes": attributes},
        "included": [
            {
                "type": "order",
                "attributes": {
                    "order_number": "0000",
                    "restaurant_name": text["restaurant"],
                },
            }
        ],
    }
