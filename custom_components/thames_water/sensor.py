"""Platform for sensor integration."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from typing import Literal
from zoneinfo import ZoneInfo

from thameswaterapi import MeterUsage, ThamesWater, meter_usage_lines_to_timeseries

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import DEFAULT_UPDATE_INTERVAL_HOURS, DOMAIN

_LOGGER = logging.getLogger(__name__)
SELENIUM_TIMEOUT = 60


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> bool:
    """Set up the Thames Water sensor platform."""
    username = entry.data["username"]
    password = entry.data["password"]
    account_number = entry.data["account_number"]
    meter_id = entry.data["meter_id"]

    unique_id = get_unique_id(meter_id)

    _LOGGER.debug(
        "Configured with username: %s, account_number: %s, meter_id: %s",
        username,
        account_number,
        meter_id,
    )

    name = entry.data.get(CONF_NAME, "Thames Water Sensor")

    sensor = ThamesWaterSensor(
        hass,
        name,
        username,
        password,
        account_number,
        meter_id,
        unique_id,
    )
    async_add_entities([sensor], update_before_add=True)

    update_interval_hours = entry.data.get(
        "update_interval_hours", DEFAULT_UPDATE_INTERVAL_HOURS
    )
    async_track_time_interval(
        hass, sensor.async_update_callback, timedelta(hours=update_interval_hours)
    )
    return True


def get_unique_id(meter_id: str) -> str:
    """Return a unique ID for the sensor."""
    return f"water_usage_{meter_id}"


def _generate_hourly_statistics_from_meter_usage(
    start: date, meter_usage: MeterUsage
) -> list[StatisticData]:
    """Convert hourly meter usage lines into StatisticData entries."""
    return [
        StatisticData(
            start=measurement.hour_start,
            state=measurement.usage,
            sum=measurement.total,
        )
        for measurement in meter_usage_lines_to_timeseries(start, meter_usage.Lines)
    ]


def _generate_daily_statistics_from_meter_usage(
    start: date, meter_usage: MeterUsage
) -> list[StatisticData]:
    """Convert daily meter usage lines into StatisticData entries."""
    tz = ZoneInfo("Europe/London")
    if isinstance(start, datetime):
        day = start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    else:
        day = datetime(start.year, start.month, start.day, tzinfo=tz)
    return [
        StatisticData(
            start=day + timedelta(days=i),
            state=int(line.Usage),
            sum=int(line.Read),
        )
        for i, line in enumerate(meter_usage.Lines)
    ]


class ThamesWaterSensor(SensorEntity):
    """Thames Water Sensor class."""

    _attr_device_class = SensorDeviceClass.WATER
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        username: str,
        password: str,
        account_number: str,
        meter_id: str,
        unique_id: str,
    ) -> None:
        """Initialize the sensor."""
        self._hass = hass
        self._name = name
        self._state: float | None = None

        self._username = username
        self._password = password
        self._account_number = account_number
        self._meter_id = meter_id

        self._unique_id = unique_id
        self._attr_should_poll = False

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this sensor."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self) -> float | None:
        """Return the sensor state (latest hourly consumption in Liters)."""
        return self._state

    @property
    def unit_of_measurement(self) -> str:
        """Return the unit of measurement (Liters)."""
        return UnitOfVolume.LITERS

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the consumption sensor."""
        return DeviceInfo(
            identifiers={(DOMAIN, "thames_water")},
            manufacturer="Thames Water",
            model="Thames Water",
            name="Thames Water Meter",
        )

    @callback
    async def async_update_callback(self, ts) -> None:
        """Callback triggered by time change to update the sensor and inject statistics."""
        await self.async_update()
        self.async_write_ha_state()

    def _fetch_meter_usage(
        self,
        start_dt: datetime,
        end_dt: datetime,
        granularity: Literal["H", "D", "M"] = "H",
    ) -> MeterUsage:
        """Fetch meter usage from Thames Water API (blocking, run in executor)."""
        thames_water = ThamesWater(
            email=self._username,
            password=self._password,
            account_number=int(self._account_number),
        )
        return thames_water.get_meter_usage(
            self._meter_id, start_dt, end_dt, granularity=granularity
        )

    def _inject_statistics(
        self,
        stat_id: str,
        name: str,
        stats: list[StatisticData],
    ) -> None:
        """Inject external statistics into the recorder."""
        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=name,
            source=DOMAIN,
            statistic_id=stat_id,
            unit_of_measurement=UnitOfVolume.LITERS,
        )
        async_add_external_statistics(self._hass, metadata, stats)

    async def async_update(self):
        """Fetch data, build statistics, and inject external statistics."""
        end_dt = (datetime.now() - timedelta(days=3)).replace(
            minute=0, second=0, microsecond=0
        )
        start_dt = end_dt - timedelta(days=3)

        try:
            hourly_usage = await self._hass.async_add_executor_job(
                self._fetch_meter_usage, start_dt, end_dt, "H"
            )
        except Exception:
            _LOGGER.exception("Failed to fetch hourly meter usage from Thames Water")
            hourly_usage = None

        try:
            daily_usage = await self._hass.async_add_executor_job(
                self._fetch_meter_usage, start_dt, end_dt, "D"
            )
        except Exception:
            _LOGGER.exception("Failed to fetch daily meter usage from Thames Water")
            daily_usage = None

        # Hourly statistics
        if hourly_usage is not None and len(hourly_usage.Lines) > 0:
            _LOGGER.info("Fetched %d hourly entries", len(hourly_usage.Lines))
            hourly_stats = _generate_hourly_statistics_from_meter_usage(
                start_dt, hourly_usage
            )
            self._state = hourly_usage.Lines[-1].Read
            self._inject_statistics(
                f"{DOMAIN}:thameswater_consumption_hourly",
                "Thames Water Consumption (Hourly)",
                hourly_stats,
            )
        else:
            hourly_stats = None
            _LOGGER.warning(
                "Thames Water returned no hourly data for %s to %s",
                start_dt,
                end_dt,
            )

        # Daily statistics
        if daily_usage is not None and len(daily_usage.Lines) > 0:
            _LOGGER.info("Fetched %d daily entries", len(daily_usage.Lines))
            daily_stats = _generate_daily_statistics_from_meter_usage(
                start_dt, daily_usage
            )
            if self._state is None:
                self._state = daily_usage.Lines[-1].Read
            self._inject_statistics(
                f"{DOMAIN}:thameswater_consumption_daily",
                "Thames Water Consumption (Daily)",
                daily_stats,
            )
        else:
            daily_stats = None
            _LOGGER.warning(
                "Thames Water returned no daily data for %s to %s",
                start_dt,
                end_dt,
            )

        # Combined: prefer hourly, fall back to daily
        combined_stats = hourly_stats or daily_stats
        if combined_stats is not None:
            self._inject_statistics(
                f"{DOMAIN}:thameswater_consumption",
                "Thames Water Consumption",
                combined_stats,
            )
