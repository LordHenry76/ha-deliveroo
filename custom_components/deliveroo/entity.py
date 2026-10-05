"""Base entity for Deliveroo."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DeliverooCoordinator


class DeliverooEntity(CoordinatorEntity[DeliverooCoordinator]):
    """Common attributes for Deliveroo entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DeliverooCoordinator, key: str) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title,
            manufacturer="Deliveroo (unofficial)",
            entry_type=DeviceEntryType.SERVICE,
        )
