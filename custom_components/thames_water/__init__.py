"""The Thames Water integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import (
    ThamesWaterConfigEntry,
    ThamesWaterCoordinator,
    ThamesWaterRuntimeData,
    ThamesWaterTariffCoordinator,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ThamesWaterConfigEntry) -> bool:
    """Set up Thames Water from a config entry."""
    coordinator = ThamesWaterCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # The tariff is a public page needing no credentials, so a failure to
    # scrape it must not take consumption and balance down with it.
    tariff_coordinator = ThamesWaterTariffCoordinator(hass, entry)
    await tariff_coordinator.async_refresh()

    entry.runtime_data = ThamesWaterRuntimeData(
        coordinator=coordinator,
        tariff_coordinator=tariff_coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ThamesWaterConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
