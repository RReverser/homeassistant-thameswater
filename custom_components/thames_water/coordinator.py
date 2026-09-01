"""Coordinators for the Thames Water integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from zoneinfo import ZoneInfo

from thameswaterapi import (
    Account,
    AuthenticationError,
    Line,
    MalformedResponse,
    RateLimitError,
    Tariff,
    TariffError,
    ThamesWater,
    get_tariff,
    lines_to_timeseries,
    meter_usage_lines_to_timeseries,
)

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import VolumeConverter

from .const import DEFAULT_UPDATE_INTERVAL_HOURS, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Meter readings are labelled in local clock time.
LONDON_TZ = ZoneInfo("Europe/London")

HOURLY_STATISTIC_ID = f"{DOMAIN}:thameswater_consumption_hourly"
DAILY_STATISTIC_ID = f"{DOMAIN}:thameswater_consumption_daily"
CONSUMPTION_STATISTIC_ID = f"{DOMAIN}:thameswater_consumption"

# The charges are a published annual scheme, but a longer cache would hide a
# break in the page parser for up to a year, and they are not guaranteed to
# change only in April: water price controls are subject to CMA
# redetermination. One unauthenticated GET on a public page either way.
TARIFF_SCAN_INTERVAL = timedelta(days=7)

# Backoff for a response that did not parse, doubling from a minute.
MIN_BACKOFF = timedelta(seconds=60)
MAX_BACKOFF = timedelta(hours=1)

# With no statistics yet there is no watermark to resume from. Thirty days is
# what the meters page itself covers.
INITIAL_HISTORY = timedelta(days=30)


@dataclass
class ThamesWaterReadings:
    """One refresh's readings, keyed by the granularity they arrived at."""

    account: Account
    hourly: dict[date, list[Line]] = field(default_factory=dict)
    daily: list[Line] = field(default_factory=list)


@dataclass
class ThamesWaterData:
    """What a refresh leaves for the entities to render."""

    account: Account
    latest_read: float | None


type ThamesWaterConfigEntry = ConfigEntry[ThamesWaterRuntimeData]


@dataclass
class ThamesWaterRuntimeData:
    """The coordinators an entry owns."""

    coordinator: ThamesWaterCoordinator
    tariff_coordinator: ThamesWaterTariffCoordinator


def days_in_range(start: date, end: date) -> list[date]:
    """Return every day from ``start`` to ``end`` inclusive."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def wants_hourly(start: date, end: date) -> bool:
    """Whether a window that wide should be asked for hour by hour.

    Hourly labels are clock times, so an hourly request only makes sense for
    a single day and a window of several days costs a request each. In steady
    state the watermark is one day behind, sometimes two; anything wider is a
    backfill and goes at daily resolution instead.
    """
    return (end - start).days <= 1


def generate_hourly_statistics(day: date, lines: list[Line]) -> list[StatisticData]:
    """Convert one day of hourly meter usage lines into StatisticData."""
    return [
        StatisticData(
            start=measurement.hour_start,
            state=measurement.usage,
            sum=measurement.total,
        )
        for measurement in meter_usage_lines_to_timeseries(day, lines)
    ]


def generate_daily_statistics(lines: list[Line]) -> list[StatisticData]:
    """Convert daily meter usage lines into StatisticData entries.

    Each date comes from its own line's dated label, so a day missing from
    the response leaves a gap rather than shifting every later reading.
    """
    return [
        StatisticData(
            start=datetime.combine(measurement.start, time.min, tzinfo=LONDON_TZ),
            state=measurement.usage,
            sum=measurement.total,
        )
        for measurement in lines_to_timeseries(lines)
    ]


def _statistic_metadata(statistic_id: str, name: str) -> StatisticMetaData:
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=VolumeConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfVolume.LITERS,
    )


class ThamesWaterCoordinator(DataUpdateCoordinator[ThamesWaterData]):
    """One authenticated session per cycle, shared by every entity."""

    config_entry: ThamesWaterConfigEntry

    def __init__(
        self, hass: HomeAssistant, config_entry: ThamesWaterConfigEntry
    ) -> None:
        """Initialize the consumption and account coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Thames Water",
            update_interval=timedelta(
                hours=config_entry.data.get(
                    "update_interval_hours", DEFAULT_UPDATE_INTERVAL_HOURS
                )
            ),
        )
        # One client for the entry's lifetime: it holds the rotating refresh
        # token, so a cycle inside the token's 24-hour life re-authenticates
        # without submitting the password again.
        self._client = ThamesWater(
            email=config_entry.data["username"],
            password=config_entry.data["password"],
            account_number=int(config_entry.data["account_number"]),
        )
        self._meter_id = config_entry.data["meter_id"]
        self._consecutive_failures = 0

    async def _async_update_data(self) -> ThamesWaterData:
        """Authenticate once, fetch what is missing, write the statistics."""
        start_day = await self._async_start_day()

        try:
            readings = await self.hass.async_add_executor_job(self._fetch, start_day)
        except AuthenticationError as err:
            # The library raises this for one condition only: the password
            # was rejected. Retrying it cannot help.
            raise ConfigEntryAuthFailed(str(err)) from err
        except RateLimitError as err:
            raise UpdateFailed(str(err), retry_after=err.retry_after) from err
        except MalformedResponse as err:
            self._consecutive_failures += 1
            backoff = min(
                MIN_BACKOFF * 2 ** (self._consecutive_failures - 1), MAX_BACKOFF
            )
            raise UpdateFailed(
                str(err), retry_after=backoff.total_seconds()
            ) from err

        self._consecutive_failures = 0
        return ThamesWaterData(
            account=readings.account,
            latest_read=self._write_statistics(readings),
        )

    async def _async_start_day(self) -> date:
        """Return the first day to ask for.

        That is the day the newest statistic falls on, re-requested rather
        than skipped: a day only partly published when it was last fetched is
        completed by writing it again. Rows carry the meter odometer, so a
        repeat overwrites with the same values.
        """
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, CONSUMPTION_STATISTIC_ID, True, set()
        )
        if not last_stat:
            return dt_util.now(LONDON_TZ).date() - INITIAL_HISTORY

        newest = dt_util.utc_from_timestamp(
            last_stat[CONSUMPTION_STATISTIC_ID][0]["start"]
        )
        return newest.astimezone(LONDON_TZ).date()

    def _fetch(self, start_day: date) -> ThamesWaterReadings:
        """Fetch this cycle's readings (blocking, run in an executor).

        The session is established up front and the data calls are made
        against it. Nothing here re-authenticates in response to a failure.
        """
        self._client.authenticate()

        today = dt_util.now(LONDON_TZ).date()
        readings = ThamesWaterReadings(account=self._client.get_account())

        if wants_hourly(start_day, today):
            for day in days_in_range(start_day, today):
                readings.hourly[day] = self._client.get_meter_usage(
                    self._meter_id, day, day, granularity="H"
                ).Lines
        else:
            # Wider than the API serves in one request, so the library splits
            # it; the response stops at the publication lag by itself.
            readings.daily = self._client.get_meter_usage_lines(
                self._meter_id, start_day, today, granularity="D"
            )

        return readings

    def _write_statistics(self, readings: ThamesWaterReadings) -> float | None:
        """Write the external statistics and return the newest meter read.

        A well-formed response with no lines is a valid answer meaning there
        is no data for that range: nothing is written and the watermark stays
        where it was, so the next cycle asks again.
        """
        hourly: list[StatisticData] = []
        for day, lines in sorted(readings.hourly.items()):
            hourly.extend(generate_hourly_statistics(day, lines))
        daily = generate_daily_statistics(readings.daily)

        if hourly:
            self._inject(HOURLY_STATISTIC_ID, "Thames Water Consumption (Hourly)", hourly)
        if daily:
            self._inject(DAILY_STATISTIC_ID, "Thames Water Consumption (Daily)", daily)

        combined = hourly or daily
        if not combined:
            _LOGGER.debug("Thames Water published no readings for this window")
            return self.data.latest_read if self.data else None

        self._inject(CONSUMPTION_STATISTIC_ID, "Thames Water Consumption", combined)
        return combined[-1]["sum"]

    def _inject(
        self, statistic_id: str, name: str, statistics: list[StatisticData]
    ) -> None:
        """Push statistics into the recorder."""
        _LOGGER.debug(
            "Injecting %d statistics for %s (%s to %s)",
            len(statistics),
            statistic_id,
            statistics[0]["start"],
            statistics[-1]["start"],
        )
        async_add_external_statistics(
            self.hass, _statistic_metadata(statistic_id, name), statistics
        )


class ThamesWaterTariffCoordinator(DataUpdateCoordinator[Tariff]):
    """Coordinator that scrapes the current Thames Water tariff."""

    config_entry: ThamesWaterConfigEntry

    def __init__(
        self, hass: HomeAssistant, config_entry: ThamesWaterConfigEntry
    ) -> None:
        """Initialize the tariff coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Thames Water tariff",
            update_interval=TARIFF_SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> Tariff:
        """Fetch and parse the tariff (blocking work runs in an executor)."""
        try:
            return await self.hass.async_add_executor_job(get_tariff)
        except TariffError as err:
            raise UpdateFailed(str(err)) from err
