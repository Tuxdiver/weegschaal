from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NUM_USERS
from .coordinator import MedisanaCoordinator, UserMeasurement


@dataclass(frozen=True)
class MedisanaBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[UserMeasurement], bool | None] = lambda m: None


BINARY_SENSOR_DESCRIPTIONS: tuple[MedisanaBinarySensorDescription, ...] = (
    MedisanaBinarySensorDescription(
        key="male",
        name="Male",
        value_fn=lambda m: m.male,
    ),
    MedisanaBinarySensorDescription(
        key="female",
        name="Female",
        value_fn=lambda m: (not m.male) if m.male is not None else None,
    ),
    MedisanaBinarySensorDescription(
        key="high_activity",
        name="High Activity",
        value_fn=lambda m: m.high_activity,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MedisanaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        MedisanaBinarySensor(coordinator, description, user_id)
        for user_id in range(1, NUM_USERS + 1)
        for description in BINARY_SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class MedisanaBinarySensor(CoordinatorEntity[MedisanaCoordinator], BinarySensorEntity):
    entity_description: MedisanaBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MedisanaCoordinator,
        description: MedisanaBinarySensorDescription,
        user_id: int,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._user_id = user_id
        self._attr_unique_id = (
            f"{coordinator.address}_{description.key}_user{user_id}"
        )
        self._attr_name = f"{description.name} User {user_id}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.address)},
            "name": f"Medisana BS444 ({coordinator.address})",
            "manufacturer": "Medisana",
            "model": "BS444",
        }

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(
            self.coordinator.get_measurement(self._user_id)
        )
