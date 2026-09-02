"""Platform for sensor integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging

from thameswaterapi import Account, Tariff

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import (
    MeterData,
    ThamesWaterConfigEntry,
    ThamesWaterCoordinator,
    ThamesWaterTariffCoordinator,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ThamesWaterMeterSensorDescription(SensorEntityDescription):
    """Describes a sensor derived from one meter's readings."""

    value_fn: Callable[[MeterData], float | datetime | None]


METER_SENSORS: tuple[ThamesWaterMeterSensorDescription, ...] = (
    ThamesWaterMeterSensorDescription(
        key="meter_reading",
        translation_key="meter_reading",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        # No state_class: this is the newest reading Thames Water has
        # published, which is days old, and the recorder would timestamp it
        # at poll time. The history lives in the external statistic.
        value_fn=lambda meter: meter.latest_read,
    ),
    ThamesWaterMeterSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda meter: meter.last_reading_at,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ThamesWaterAccountSensorDescription(SensorEntityDescription):
    """Describes a sensor derived from one contract account."""

    value_fn: Callable[[Account], float | None]


ACCOUNT_SENSORS: tuple[ThamesWaterAccountSensorDescription, ...] = (
    ThamesWaterAccountSensorDescription(
        key="outstanding_balance",
        translation_key="outstanding_balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="GBP",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda account: float(account.paymentDueAmount),
    ),
)


@dataclass(frozen=True, kw_only=True)
class ThamesWaterTariffSensorDescription(SensorEntityDescription):
    """Describes a Thames Water tariff sensor."""

    value_fn: Callable[[Tariff], float]


TARIFF_SENSORS: tuple[ThamesWaterTariffSensorDescription, ...] = (
    ThamesWaterTariffSensorDescription(
        key="unit_rate",
        translation_key="unit_rate",
        native_unit_of_measurement="GBP/L",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=6,
        value_fn=lambda tariff: tariff.unit_rate_per_litre,
    ),
    ThamesWaterTariffSensorDescription(
        key="standing_charge",
        translation_key="standing_charge",
        native_unit_of_measurement="GBP/day",
        icon="mdi:cash-clock",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda tariff: tariff.standing_charge_per_day,
    ),
    ThamesWaterTariffSensorDescription(
        key="volumetric_rate",
        translation_key="volumetric_rate",
        native_unit_of_measurement="GBP/m³",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda tariff: tariff.volumetric_rate_per_m3,
    ),
    ThamesWaterTariffSensorDescription(
        key="clean_water_rate",
        translation_key="clean_water_rate",
        native_unit_of_measurement="GBP/m³",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.clean_water_rate_per_m3,
    ),
    ThamesWaterTariffSensorDescription(
        key="wastewater_rate",
        translation_key="wastewater_rate",
        native_unit_of_measurement="GBP/m³",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.wastewater_rate_per_m3,
    ),
    ThamesWaterTariffSensorDescription(
        key="water_fixed_charge",
        translation_key="water_fixed_charge",
        native_unit_of_measurement="GBP/year",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.water_fixed_per_year,
    ),
    ThamesWaterTariffSensorDescription(
        key="wastewater_fixed_charge",
        translation_key="wastewater_fixed_charge",
        native_unit_of_measurement="GBP/year",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        value_fn=lambda tariff: tariff.wastewater_fixed_per_year,
    ),
)


def account_device_info(account: Account) -> DeviceInfo:
    """Return the device for one contract account, named by its address."""
    address = account.property.address if account.property else None
    name = (address.fullAddress if address else None) or (
        f"Account {account.contractAccountNumber}"
    )
    return DeviceInfo(
        identifiers={(DOMAIN, f"account_{account.contractAccountNumber}")},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer="Thames Water",
        model="Contract account",
        name=name,
    )


def meter_device_info(meter: MeterData) -> DeviceInfo:
    """Return the device for one meter, hanging off its account."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"meter_{meter.meter_id}")},
        entry_type=DeviceEntryType.SERVICE,
        manufacturer="Thames Water",
        model="Water meter",
        name=f"Meter {meter.meter_id}",
        via_device=(DOMAIN, f"account_{meter.account_number}"),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ThamesWaterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Thames Water sensor platform."""
    data = entry.runtime_data
    coordinator = data.coordinator

    async_add_entities(
        ThamesWaterTariffSensor(data.tariff_coordinator, description)
        for description in TARIFF_SENSORS
    )

    known: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        """Add entities for accounts and meters not yet seen.

        Accounts and meters are discovered on every refresh, so a meter added
        to the login later turns up without the entry being reconfigured.
        """
        entities: list[SensorEntity] = []

        for account_number, account in coordinator.data.accounts.items():
            key = f"account_{account_number}"
            if key in known:
                continue
            known.add(key)
            entities.extend(
                ThamesWaterAccountSensor(coordinator, account_number, description)
                for description in ACCOUNT_SENSORS
            )

        for meter_id in coordinator.data.meters:
            key = f"meter_{meter_id}"
            if key in known:
                continue
            known.add(key)
            entities.extend(
                ThamesWaterMeterSensor(coordinator, meter_id, description)
                for description in METER_SENSORS
            )

        if entities:
            async_add_entities(entities)

    _add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))


class ThamesWaterMeterSensor(CoordinatorEntity[ThamesWaterCoordinator], SensorEntity):
    """A sensor derived from one meter's readings."""

    _attr_has_entity_name = True
    entity_description: ThamesWaterMeterSensorDescription

    def __init__(
        self,
        coordinator: ThamesWaterCoordinator,
        meter_id: str,
        description: ThamesWaterMeterSensorDescription,
    ) -> None:
        """Initialize the meter sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._meter_id = meter_id
        self._attr_unique_id = f"{meter_id}_{description.key}"
        self._attr_device_info = meter_device_info(self._meter)

    @property
    def _meter(self) -> MeterData:
        return self.coordinator.data.meters[self._meter_id]

    @property
    def available(self) -> bool:
        """Whether the meter was still on the account at the last refresh."""
        return super().available and self._meter_id in self.coordinator.data.meters

    @property
    def native_value(self) -> float | datetime | None:
        """Return the value derived from this meter's latest readings."""
        return self.entity_description.value_fn(self._meter)


class ThamesWaterAccountSensor(CoordinatorEntity[ThamesWaterCoordinator], SensorEntity):
    """A sensor derived from one contract account."""

    _attr_has_entity_name = True
    entity_description: ThamesWaterAccountSensorDescription

    def __init__(
        self,
        coordinator: ThamesWaterCoordinator,
        account_number: int,
        description: ThamesWaterAccountSensorDescription,
    ) -> None:
        """Initialize the account sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._account_number = account_number
        self._attr_unique_id = f"{account_number}_{description.key}"
        self._attr_device_info = account_device_info(self._account)

    @property
    def _account(self) -> Account:
        return self.coordinator.data.accounts[self._account_number]

    @property
    def available(self) -> bool:
        """Whether the account was still on the login at the last refresh."""
        return super().available and self._account_number in self.coordinator.data.accounts

    @property
    def native_value(self) -> float | None:
        """Return the value derived from this account."""
        return self.entity_description.value_fn(self._account)

    @property
    def extra_state_attributes(self) -> dict[str, float | bool | None]:
        """Expose the broader balance picture as attributes."""
        account = self._account
        return {
            "current_balance": float(account.currentBalance),
            "is_in_credit": account.isInCredit,
        }


class ThamesWaterTariffSensor(
    CoordinatorEntity[ThamesWaterTariffCoordinator], RestoreSensor
):
    """A sensor derived from the scraped Thames Water tariff.

    The figures change about once a year, so the last known ones are worth
    more than nothing while the page is unreachable or a restart is in
    progress; they are restored until a scrape succeeds.
    """

    _attr_has_entity_name = True
    entity_description: ThamesWaterTariffSensorDescription

    def __init__(
        self,
        coordinator: ThamesWaterTariffCoordinator,
        description: ThamesWaterTariffSensorDescription,
    ) -> None:
        """Initialize the tariff sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"tariff_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "tariff")},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Thames Water",
            model="Tariff",
            name="Thames Water tariff",
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
