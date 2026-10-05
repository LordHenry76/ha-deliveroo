"""Sensors for Deliveroo."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import DeliverooOrderStatus
from .coordinator import DeliverooConfigEntry, DeliverooCoordinator
from .entity import DeliverooEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DeliverooSensorDescription(SensorEntityDescription):
    """Describes a Deliveroo sensor."""

    value_fn: Callable[[DeliverooOrderStatus], Any]
    attributes_fn: Callable[[DeliverooOrderStatus], dict[str, Any]] | None = None
    idle_value: Any = None


SENSORS: tuple[DeliverooSensorDescription, ...] = (
    DeliverooSensorDescription(
        key="status",
        translation_key="status",
        value_fn=lambda s: s.state,
        idle_value="idle",
        attributes_fn=lambda s: {
            "order_id": s.order_id,
            "order_number": s.order_number,
            "ui_status": s.ui_status,
            "eta_status": s.eta_status,
            "rider_route": s.rider_route,
            "rider_status": s.rider_status,
            "updated_at": s.updated_at,
        },
    ),
    DeliverooSensorDescription(
        key="step",
        translation_key="step",
        value_fn=lambda s: s.step_title,
        attributes_fn=lambda s: {
            "step_index": s.step_index,
            "step_count": len(s.steps) or None,
            "steps": [step.title for step in s.steps],
        },
    ),
    DeliverooSensorDescription(
        key="message",
        translation_key="message",
        value_fn=lambda s: s.message,
        attributes_fn=lambda s: {"advisory": s.advisory},
    ),
    DeliverooSensorDescription(
        key="eta",
        translation_key="eta",
        value_fn=lambda s: s.eta,
    ),
    DeliverooSensorDescription(
        key="estimated_delivery",
        translation_key="estimated_delivery",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: s.estimated_delivery,
    ),
    DeliverooSensorDescription(
        key="progress",
        translation_key="progress",
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.progress,
    ),
    DeliverooSensorDescription(
        key="rider_code",
        translation_key="rider_code",
        value_fn=lambda s: s.rider_code,
    ),
    DeliverooSensorDescription(
        key="restaurant",
        translation_key="restaurant",
        value_fn=lambda s: s.restaurant_name,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DeliverooConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Deliveroo sensors."""
    coordinator = entry.runtime_data
    async_add_entities(DeliverooSensor(coordinator, desc) for desc in SENSORS)


class DeliverooSensor(DeliverooEntity, SensorEntity):
    """A Deliveroo order sensor."""

    entity_description: DeliverooSensorDescription

    def __init__(
        self,
        coordinator: DeliverooCoordinator,
        description: DeliverooSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        status = self.coordinator.data.status
        if status is None:
            return self.entity_description.idle_value
        return self.entity_description.value_fn(status)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, if the sensor defines any."""
        attributes_fn = self.entity_description.attributes_fn
        if attributes_fn is None:
            return None
        status = self.coordinator.data.status
        if status is None:
            return None
        return attributes_fn(status)
