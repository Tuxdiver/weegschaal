from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NUM_USERS
from .coordinator import MedisanaCoordinator, UserMeasurement


@dataclass(frozen=True)
class MedisanaSensorDescription(SensorEntityDescription):
    value_fn: Callable[[UserMeasurement], float | int | None] = lambda m: None


SENSOR_DESCRIPTIONS: tuple[MedisanaSensorDescription, ...] = (
    MedisanaSensorDescription(
        key="weight",
        name="Weight",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda m: m.weight,
    ),
    MedisanaSensorDescription(
        key="bmi",
        name="BMI",
        native_unit_of_measurement=None,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m: m.bmi,
    ),
    MedisanaSensorDescription(
        key="kcal",
        name="kcal",
        native_unit_of_measurement="kcal",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.kcal,
    ),
    MedisanaSensorDescription(
        key="fat",
        name="Body Fat",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m: m.fat,
    ),
    MedisanaSensorDescription(
        key="tbw",
        name="Water",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m: m.tbw,
    ),
    MedisanaSensorDescription(
        key="muscle",
        name="Muscle",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m: m.muscle,
    ),
    MedisanaSensorDescription(
        key="bone",
        name="Bone Mass",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m: m.bone,
    ),
    MedisanaSensorDescription(
        key="age",
        name="Age",
        native_unit_of_measurement="years",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.age,
    ),
    MedisanaSensorDescription(
        key="size",
        name="Height",
        native_unit_of_measurement="cm",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda m: m.size_cm,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MedisanaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        MedisanaSensor(coordinator, description, user_id)
        for user_id in range(1, NUM_USERS + 1)
        for description in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)


class MedisanaSensor(CoordinatorEntity[MedisanaCoordinator], SensorEntity):
    entity_description: MedisanaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MedisanaCoordinator,
        description: MedisanaSensorDescription,
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
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(
            self.coordinator.get_measurement(self._user_id)
        )
