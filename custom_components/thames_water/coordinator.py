"""Coordinators for the Thames Water integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from zoneinfo import ZoneInfo

from thameswaterapi import (
    Account,
    AuthenticationError,
    Line,
    Tariff,
    TariffError,
    ThamesWater,
    get_tariff,
    meter_usage_lines_to_timeseries,
    tariff_for,
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
    statistics_during_period,
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
CONSUMPTION_STATISTIC_ID = f"{DOMAIN}:thameswater_consumption"
COST_STATISTIC_ID = f"{DOMAIN}:thameswater_consumption_cost"

# The tariff is a fixed annual published scheme, so once a day is ample.
TARIFF_SCAN_INTERVAL = timedelta(hours=24)

# With no statistics yet there is no watermark to resume from. Thirty days is
# what the meters page itself covers.
INITIAL_HISTORY = timedelta(days=30)


@dataclass
class ThamesWaterReadings:
    """One refresh's readings and the day the window they cover starts on."""

    account: Account
    start_day: date
    lines: list[Line]


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


def generate_hourly_statistics(
    start_day: date, lines: list[Line]
) -> list[StatisticData]:
    """Convert a window of hourly meter usage lines into StatisticData.

    The library timestamps each reading by taking the hour from its label
    and the day from a cursor that advances at every midnight, so the window
    can be as wide as the caller likes.
    """
    return [
        StatisticData(
            start=measurement.hour_start,
            state=measurement.usage,
            sum=measurement.total,
        )
        for measurement in meter_usage_lines_to_timeseries(start_day, lines)
    ]


def charges_for(days: set[date]) -> dict[date, Tariff]:
    """Look up the charges in force on each day.

    Blocking, because a day past the published table sends `tariff_for` to
    the help page. A day nothing covers is left out, and the readings on it
    go unpriced rather than taking a rate that was not theirs.
    """
    charges = {}
    for day in days:
        try:
            charges[day] = tariff_for(day)
        except TariffError:
            _LOGGER.debug("No published charges cover %s", day)
    return charges


def price_readings(
    consumption: list[StatisticData],
    charges: dict[date, Tariff],
    priced_through: datetime | None,
    running_total: float,
) -> list[StatisticData]:
    """Price each reading at the rate in force on its own date.

    A reading already priced is never priced again, because cost
    accumulates while a consumption sum is the meter's own odometer.
    """
    rows: list[StatisticData] = []

    for reading in consumption:
        if priced_through is not None and reading["start"] <= priced_through:
            continue
        tariff = charges.get(reading["start"].astimezone(LONDON_TZ).date())
        if tariff is None:
            continue
        running_total += reading["state"] * tariff.unit_rate_per_litre
        rows.append(
            StatisticData(
                start=reading["start"],
                state=round(reading["state"] * tariff.unit_rate_per_litre, 4),
                sum=round(running_total, 4),
            )
        )

    return rows


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
        self._meter_id = config_entry.data["meter_id"]

    async def _async_update_data(self) -> ThamesWaterData:
        """Log in once, fetch what is missing, write the statistics."""
        start_day = await self._async_start_day()

        try:
            readings = await self.hass.async_add_executor_job(self._fetch, start_day)
        except AuthenticationError as err:
            # A rejected password is the one failure retrying cannot fix.
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(str(err)) from err

        consumption = self._write_statistics(readings)
        await self._async_write_cost_statistics(consumption)

        return ThamesWaterData(
            account=readings.account,
            latest_read=(
                consumption[-1]["sum"]
                if consumption
                else (self.data.latest_read if self.data else None)
            ),
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

        Constructing the client logs in, so every data call below runs
        against that one session.
        """
        client = ThamesWater(
            email=self.config_entry.data["username"],
            password=self.config_entry.data["password"],
            account_number=int(self.config_entry.data["account_number"]),
        )

        today = dt_util.now(LONDON_TZ).date()

        # One request, whatever the window is: a response ending today is
        # truncated on a whole-day boundary rather than padded, so the days
        # not yet published are simply absent from it.
        _LOGGER.debug("Asking for hourly readings from %s to %s", start_day, today)
        usage = client.get_meter_usage(
            self._meter_id, start_day, today, granularity="H"
        )

        return ThamesWaterReadings(
            account=client.get_account(),
            start_day=start_day,
            lines=usage.Lines,
        )

    def _write_statistics(self, readings: ThamesWaterReadings) -> list[StatisticData]:
        """Write the consumption statistics and return the rows written.

        A well-formed response with no lines is a valid answer meaning there
        is no data for that range: nothing is written and the watermark stays
        where it was, so the next cycle asks again.
        """
        hourly = generate_hourly_statistics(readings.start_day, readings.lines)
        if not hourly:
            _LOGGER.debug("Thames Water published no readings for this window")
            return []

        self._inject(HOURLY_STATISTIC_ID, "Thames Water Consumption (Hourly)", hourly)
        self._inject(CONSUMPTION_STATISTIC_ID, "Thames Water Consumption", hourly)
        return hourly

    async def _async_write_cost_statistics(
        self, consumption: list[StatisticData]
    ) -> None:
        """Write what each of those readings cost.

        Readings arrive about three days late and a backfill covers a month,
        so a window can span a price change. The Energy dashboard's own cost
        sensor prices whatever the meter reports at the rate showing when it
        reports it, which cannot put last year's rate on last year's water.
        """
        if not consumption:
            return

        priced_through, running_total = await self._async_cost_watermark()
        if priced_through is None:
            # Nothing has ever been priced, and the readings already in the
            # recorder are worth as much as the ones just fetched. Upstream
            # wrote no cost statistic at all, so an entry upgrading to this
            # version has a full history waiting and no rows to conflict with.
            consumption = await self._async_all_consumption(consumption)

        days = {row["start"].astimezone(LONDON_TZ).date() for row in consumption}
        charges = await self.hass.async_add_executor_job(charges_for, days)
        rows = price_readings(consumption, charges, priced_through, running_total)
        if not rows:
            return

        _LOGGER.debug(
            "Injecting %d cost statistics (%s to %s)",
            len(rows),
            rows[0]["start"],
            rows[-1]["start"],
        )
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name="Thames Water Consumption Cost",
                source=DOMAIN,
                statistic_id=COST_STATISTIC_ID,
                unit_class=None,
                unit_of_measurement="GBP",
            ),
            rows,
        )

    async def _async_all_consumption(
        self, fetched: list[StatisticData]
    ) -> list[StatisticData]:
        """Return every consumption reading on record, newest write winning.

        The rows just fetched are queued for the recorder rather than in it,
        so they are merged over what a read returns instead of replacing it.
        """
        history = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            dt_util.utc_from_timestamp(0),
            None,
            {CONSUMPTION_STATISTIC_ID},
            "hour",
            None,
            {"state", "sum"},
        )

        readings = {
            dt_util.utc_from_timestamp(row["start"]): StatisticData(
                start=dt_util.utc_from_timestamp(row["start"]),
                state=row["state"],
                sum=row["sum"],
            )
            for row in history.get(CONSUMPTION_STATISTIC_ID, [])
        }
        readings.update({row["start"]: row for row in fetched})

        _LOGGER.debug(
            "Pricing %d readings, none having been priced before", len(readings)
        )
        return [readings[start] for start in sorted(readings)]

    async def _async_cost_watermark(self) -> tuple[datetime | None, float]:
        """Return how far cost has been priced, and the total to carry on from.

        Cost accumulates, unlike consumption, whose sum is the meter's own
        odometer, so a reading already priced must not be priced again when
        the day holding the watermark is re-requested.
        """
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, COST_STATISTIC_ID, True, {"sum"}
        )
        if not last_stat:
            return None, 0.0

        row = last_stat[COST_STATISTIC_ID][0]
        return dt_util.utc_from_timestamp(row["start"]), row.get("sum") or 0.0

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
