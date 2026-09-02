"""The Thames Water integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import (
    ThamesWaterConfigEntry,
    ThamesWaterCoordinator,
    ThamesWaterRuntimeData,
    ThamesWaterTariffCoordinator,
    end_session,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ThamesWaterConfigEntry) -> bool:
    """Set up Thames Water from a config entry."""
    # The tariff is a public page needing no credentials, so a failure to
    # scrape it must not take consumption and balance down with it. It is
    # fetched first because the consumption coordinator prices its readings
    # with it.
    tariff_coordinator = ThamesWaterTariffCoordinator(hass, entry)
    await tariff_coordinator.async_refresh()

    coordinator = ThamesWaterCoordinator(hass, entry, tariff_coordinator)
    await coordinator.async_config_entry_first_refresh()

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


async def async_remove_entry(
    hass: HomeAssistant, entry: ThamesWaterConfigEntry
) -> None:
    """End the Thames Water session when the entry is deleted."""
    try:
        await hass.async_add_executor_job(end_session, entry.data)
    except Exception as err:  # noqa: BLE001
        # Nothing depends on the result: the session expires by itself.
        _LOGGER.debug("Signing out of Thames Water failed: %s", err)
