"""Rider position for Deliveroo (shown on the map while the order is on its way)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import DeliverooRider
from .coordinator import DeliverooConfigEntry
from .entity import DeliverooEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeliverooConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Deliveroo rider tracker."""
    async_add_entities([DeliverooRiderTracker(entry.runtime_data, "rider")])


class DeliverooRiderTracker(DeliverooEntity, TrackerEntity):
    """Live GPS position of the rider; unknown when Deliveroo sends none."""

    _attr_translation_key = "rider"
    # The route changes on every poll: keep it out of the recorder.
    _unrecorded_attributes = frozenset({"route"})

    @property
    def _rider(self) -> DeliverooRider | None:
        status = self.coordinator.data.status
        return status.rider if status is not None else None

    @property
    def latitude(self) -> float | None:
        """Return the rider latitude."""
        rider = self._rider
        return rider.latitude if rider and rider.position else None

    @property
    def longitude(self) -> float | None:
        """Return the rider longitude."""
        rider = self._rider
        return rider.longitude if rider and rider.position else None

    @property
    def entity_picture(self) -> str | None:
        """Vehicle icon used by Deliveroo's own map."""
        rider = self._rider
        return rider.icon_url if rider and rider.position else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Vehicle, route phase and the remaining route as [lat, lon] pairs."""
        status = self.coordinator.data.status
        rider = self._rider
        if status is None or rider is None or rider.position is None:
            return None
        return {
            "vehicle_type": rider.vehicle_type,
            "rider_route": status.rider_route,
            "rider_status": status.rider_status,
            "route": [list(point) for point in rider.route],
        }
