"""Buttons for Deliveroo."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DeliverooConfigEntry
from .entity import DeliverooEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeliverooConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Deliveroo buttons."""
    async_add_entities([DeliverooRefreshButton(entry.runtime_data, "refresh")])


class DeliverooRefreshButton(DeliverooEntity, ButtonEntity):
    """Check for a new order right now instead of waiting for the next poll."""

    _attr_translation_key = "refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """Stay pressable even if the last poll failed."""
        return True

    async def async_press(self) -> None:
        """Re-read the order history now."""
        await self.coordinator.async_force_refresh()
