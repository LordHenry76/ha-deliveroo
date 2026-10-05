"""Binary sensors for Deliveroo."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DeliverooConfigEntry
from .entity import DeliverooEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeliverooConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Deliveroo binary sensors."""
    async_add_entities([DeliverooActiveOrder(entry.runtime_data, "order_active")])


class DeliverooActiveOrder(DeliverooEntity, BinarySensorEntity):
    """On while an order is being prepared or delivered."""

    _attr_translation_key = "order_active"

    @property
    def is_on(self) -> bool:
        """Return True if there is an active order."""
        return self.coordinator.data.active_order_id is not None
