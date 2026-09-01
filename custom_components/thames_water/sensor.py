"""Platform for sensor integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from thameswaterapi import Tariff

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CONF_NAME, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import (
    ThamesWaterConfigEntry,
    ThamesWaterCoordinator,
    ThamesWaterTariffCoordinator,
)

_LOGGER = logging.getLogger(__name__)

METER_DEVICE_INFO = DeviceInfo(
    identifiers={(DOMAIN, "thames_water")},
    manufacturer="Thames Water",
    model="Thames Water",
    name="Thames Water Meter",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ThamesWaterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Thames Water sensor platform."""
    data = entry.runtime_data

    _LOGGER.debug(
        "Configured with username: %s, account_number: %s, meter_id: %s",
        entry.data["username"],
        entry.data["account_number"],
        entry.data["meter_id"],
    )

    async_add_entities(
        [
            ThamesWaterSensor(
                data.coordinator,
                entry.data.get(CONF_NAME, "Thames Water Sensor"),
                get_unique_id(entry.data["meter_id"]),
            ),
            ThamesWaterBalanceSensor(
                data.coordinator, entry.data["account_number"]
            ),
            *(
                ThamesWaterTariffSensor(data.tariff_coordinator, description)
                for description in TARIFF_SENSORS
            ),
        ]
    )


def get_unique_id(meter_id: str) -> str:
    """Return a unique ID for the sensor."""
    return f"water_usage_{meter_id}"


class ThamesWaterSensor(CoordinatorEntity[ThamesWaterCoordinator], SensorEntity):
    """Thames Water Sensor class."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_device_info = METER_DEVICE_INFO

    def __init__(
        self,
        coordinator: ThamesWaterCoordinator,
        name: str,
        unique_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = unique_id

    @property
    def native_value(self) -> float | None:
        """Return the newest meter read, in litres."""
        return self.coordinator.data.latest_read


class ThamesWaterBalanceSensor(CoordinatorEntity[ThamesWaterCoordinator], SensorEntity):
    """Sensor exposing the outstanding balance on the Thames Water account."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_name = "Thames Water Outstanding Balance"
    _attr_device_info = METER_DEVICE_INFO

    def __init__(
        self, coordinator: ThamesWaterCoordinator, account_number: str
    ) -> None:
        """Initialize the balance sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"thames_water_balance_{account_number}"

    @property
    def native_value(self) -> float | None:
        """Return the amount currently due."""
        return float(self.coordinator.data.account.paymentDueAmount)

    @property
    def extra_state_attributes(self) -> dict[str, float | bool | None]:
        """Expose the broader balance picture as attributes."""
        account = self.coordinator.data.account
        return {
            "current_balance": float(account.currentBalance),
            "is_in_credit": account.isInCredit,
        }


@dataclass(frozen=True, kw_only=True)
class ThamesWaterTariffSensorDescription(SensorEntityDescription):
    """Describes a Thames Water tariff sensor."""

    value_fn: Callable[[Tariff], float]


TARIFF_SENSORS: tuple[ThamesWaterTariffSensorDescription, ...] = (
    ThamesWaterTariffSensorDescription(
        key="unit_rate",
        name="Thames Water Unit Rate",
        native_unit_of_measurement="GBP/L",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=6,
        value_fn=lambda tariff: tariff.unit_rate_per_litre,
    ),
    ThamesWaterTariffSensorDescription(
        key="standing_charge",
        name="Thames Water Standing Charge",
        native_unit_of_measurement="GBP/day",
        icon="mdi:cash-clock",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda tariff: tariff.standing_charge_per_day,
    ),
    ThamesWaterTariffSensorDescription(
        key="volumetric_rate",
        name="Thames Water Volumetric Rate",
        native_unit_of_measurement="GBP/m³",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda tariff: tariff.volumetric_rate_per_m3,
    ),
    ThamesWaterTariffSensorDescription(
        key="clean_water_rate",
        name="Thames Water Clean Water Rate",
        native_unit_of_measurement="GBP/m³",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.clean_water_rate_per_m3,
    ),
    ThamesWaterTariffSensorDescription(
        key="wastewater_rate",
        name="Thames Water Wastewater Rate",
        native_unit_of_measurement="GBP/m³",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.wastewater_rate_per_m3,
    ),
    ThamesWaterTariffSensorDescription(
        key="water_fixed_charge",
        name="Thames Water Water Fixed Charge",
        native_unit_of_measurement="GBP/year",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.water_fixed_per_year,
    ),
    ThamesWaterTariffSensorDescription(
        key="wastewater_fixed_charge",
        name="Thames Water Wastewater Fixed Charge",
        native_unit_of_measurement="GBP/year",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.wastewater_fixed_per_year,
    ),
)


class ThamesWaterTariffSensor(
    CoordinatorEntity[ThamesWaterTariffCoordinator], RestoreSensor
):
    """A sensor derived from the scraped Thames Water tariff.

    The figures change about once a year, so the last known ones are worth
    more than nothing while the page is unreachable or a restart is in
    progress; they are restored until a scrape succeeds.
    """

    entity_description: ThamesWaterTariffSensorDescription

    def __init__(
        self,
        coordinator: ThamesWaterTariffCoordinator,
        description: ThamesWaterTariffSensorDescription,
    ) -> None:
        """Initialize the tariff sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_name = description.name
        self._attr_unique_id = f"thames_water_tariff_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "thames_water_tariff")},
            manufacturer="Thames Water",
            model="Tariff",
            name="Thames Water Tariff",
        )
        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known figure for use until a scrape succeeds."""
        await super().async_added_to_hass()
        if (last_data := await self.async_get_last_sensor_data()) is not None:
            self._restored_value = last_data.native_value

    @property
    def native_value(self) -> float | None:
        """Return the value derived from the current tariff."""
        if self.coordinator.data is None:
            return self._restored_value
        return self.entity_description.value_fn(self.coordinator.data)
