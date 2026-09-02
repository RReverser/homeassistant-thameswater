"""Coordinators for the Thames Water integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.unit_conversion import VolumeConverter

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Meter readings are labelled in local clock time.
LONDON_TZ = ZoneInfo("Europe/London")

# Readings are published daily and arrive about three days late, so nothing
# depends on this value. Under 24 hours the refresh token never expires
# between cycles, which is what keeps the password out of the steady state.
# A user wanting another cadence turns polling off in the entry's system
# options and calls homeassistant.update_entity from an automation.
UPDATE_INTERVAL = timedelta(hours=12)

# The charges are a published annual scheme, but a longer cache would hide a
# break in the page parser for up to a year, and they are not guaranteed to
# change only in April: water price controls are subject to CMA
# redetermination. One unauthenticated GET on a public page either way.
TARIFF_SCAN_INTERVAL = timedelta(days=7)

# Backoff for a response that did not parse, doubling from a minute.
MIN_BACKOFF = timedelta(seconds=60)
MAX_BACKOFF = timedelta(hours=1)

# With no statistics yet there is no watermark to resume from. Deeper history
# is available and is what the import_history action is for.
INITIAL_HISTORY = timedelta(days=30)


def consumption_statistic_id(meter_id: str) -> str:
    """Return the consumption statistic ID for one meter."""
    return f"{DOMAIN}:{slugify(meter_id)}_consumption"


def cost_statistic_id(meter_id: str) -> str:
    """Return the cost statistic ID for one meter."""
    return f"{DOMAIN}:{slugify(meter_id)}_cost"


@dataclass
class MeterReadings:
    """One meter's readings from one refresh."""

    meter_id: str
    account_number: int
    start_day: date
    lines: list[Line]


@dataclass
class MeterData:
    """What a refresh leaves for one meter's entities to render."""

    meter_id: str
    account_number: int
    latest_read: float | None = None
    last_reading_at: datetime | None = None


@dataclass
class ThamesWaterData:
    """What a refresh leaves for the entities to render."""

    accounts: dict[int, Account] = field(default_factory=dict)
    meters: dict[str, MeterData] = field(default_factory=dict)


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


def price_readings(
    consumption: list[StatisticData],
    tariff: Tariff,
    priced_through: datetime | None,
    running_total: float,
) -> tuple[list[StatisticData], int]:
    """Price each reading, returning the cost rows and how many were skipped.

    A reading already priced is never priced again, because cost accumulates
    while a consumption sum is the meter's own odometer. A reading from
    before the tariff took effect is left unpriced rather than valued at a
    rate that was not in force for it; only the current rate is published.
    """
    rows: list[StatisticData] = []
    unpriced = 0

    for reading in consumption:
        if priced_through is not None and reading["start"] <= priced_through:
            continue
        if reading["start"].astimezone(LONDON_TZ).date() < tariff.effective_date:
            unpriced += 1
            continue
        cost = reading["state"] * tariff.unit_rate_per_litre
        running_total += cost
        rows.append(
            StatisticData(
                start=reading["start"],
                state=round(cost, 4),
                sum=round(running_total, 4),
            )
        )

    return rows, unpriced


class ThamesWaterCoordinator(DataUpdateCoordinator[ThamesWaterData]):
    """One authenticated session per cycle, serving every meter on the login."""

    config_entry: ThamesWaterConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ThamesWaterConfigEntry,
        tariff_coordinator: ThamesWaterTariffCoordinator,
    ) -> None:
        """Initialize the consumption and account coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name="Thames Water",
            update_interval=UPDATE_INTERVAL,
        )
        # One client for the entry's lifetime: it holds the rotating refresh
        # token, so a cycle inside the token's 24-hour life re-authenticates
        # without submitting the password again.
        self._client = ThamesWater(
            email=config_entry.data[CONF_USERNAME],
            password=config_entry.data[CONF_PASSWORD],
        )
        self._tariff_coordinator = tariff_coordinator
        self._consecutive_failures = 0

    async def _async_update_data(self) -> ThamesWaterData:
        """Authenticate once, fetch every meter, write the statistics."""
        start_days = await self._async_start_days()

        try:
            accounts, readings = await self.hass.async_add_executor_job(
                self._fetch, start_days
            )
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
            raise UpdateFailed(str(err), retry_after=backoff.total_seconds()) from err

        self._consecutive_failures = 0

        data = ThamesWaterData(accounts=accounts)
        for meter_readings in readings:
            data.meters[meter_readings.meter_id] = await self._async_record(
                meter_readings
            )
        return data

    async def async_import_history(self, start_day: date) -> None:
        """Import every meter's readings from ``start_day`` onwards.

        Re-importable: a consumption row carries the meter odometer, so
        writing one again overwrites it with the same value.
        """
        start_days: dict[str, date] = {}
        _, readings = await self.hass.async_add_executor_job(
            self._fetch, start_days, start_day
        )
        for meter_readings in readings:
            await self._async_record(meter_readings)

    async def _async_record(self, readings: MeterReadings) -> MeterData:
        """Write one meter's statistics and summarise them for its entities."""
        consumption = self._write_statistics(readings)
        await self._async_write_cost_statistics(readings.meter_id, consumption)

        previous = (self.data.meters if self.data else {}).get(readings.meter_id)
        if not consumption:
            return previous or MeterData(
                meter_id=readings.meter_id, account_number=readings.account_number
            )

        return MeterData(
            meter_id=readings.meter_id,
            account_number=readings.account_number,
            latest_read=consumption[-1]["sum"],
            last_reading_at=consumption[-1]["start"],
        )

    async def _async_start_days(self) -> dict[str, date]:
        """Return each known meter's resume watermark, keyed by serial.

        A meter is asked for from the day its newest statistic falls on,
        re-requested rather than skipped: a day only partly published when it
        was last fetched is completed by writing it again, and the rows carry
        the meter odometer, so a repeat overwrites with the same values.
        """
        meters = list(self.data.meters) if self.data else []
        if not meters:
            return {}

        recorder = get_instance(self.hass)
        start_days = {}
        for meter_id in meters:
            statistic_id = consumption_statistic_id(meter_id)
            last_stat = await recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, True, set()
            )
            if last_stat:
                newest = dt_util.utc_from_timestamp(last_stat[statistic_id][0]["start"])
                start_days[meter_id] = newest.astimezone(LONDON_TZ).date()
        return start_days

    def _fetch(
        self,
        start_days: dict[str, date],
        default_start: date | None = None,
    ) -> tuple[dict[int, Account], list[MeterReadings]]:
        """Fetch every account and meter (blocking, run in an executor).

        The session is established up front and the data calls are made
        against it. Nothing here re-authenticates in response to a failure.
        """
        self._client.authenticate()

        today = dt_util.now(LONDON_TZ).date()
        fallback = default_start or today - INITIAL_HISTORY

        accounts: dict[int, Account] = {}
        readings: list[MeterReadings] = []

        # Contract accounts come from the ID token and the meters on each from
        # getMeters, so a meter added later appears on its own.
        for account_number in self._client.get_account_numbers():
            # Assigning re-scopes the session, one request per account.
            self._client.account_number = account_number
            accounts[account_number] = self._client.get_account()

            for meter_id in self._client.get_meter_numbers():
                start_day = start_days.get(meter_id, fallback)
                _LOGGER.debug(
                    "Asking for hourly readings for meter %s from %s to %s",
                    meter_id,
                    start_day,
                    today,
                )
                # One request, whatever the window is: a response ending today
                # is truncated on a whole-day boundary rather than padded, so
                # the days not yet published are simply absent from it.
                usage = self._client.get_meter_usage(
                    meter_id, start_day, today, granularity="H"
                )
                readings.append(
                    MeterReadings(
                        meter_id=meter_id,
                        account_number=account_number,
                        start_day=start_day,
                        lines=usage.Lines,
                    )
                )

        return accounts, readings

    def _write_statistics(self, readings: MeterReadings) -> list[StatisticData]:
        """Write one meter's consumption statistics and return the rows.

        A well-formed response with no lines is a valid answer meaning there
        is no data for that range: nothing is written and the watermark stays
        where it was, so the next cycle asks again.
        """
        consumption = generate_hourly_statistics(readings.start_day, readings.lines)
        if not consumption:
            _LOGGER.debug(
                "Thames Water published no readings for meter %s in this window",
                readings.meter_id,
            )
            return []

        statistic_id = consumption_statistic_id(readings.meter_id)
        _LOGGER.debug(
            "Injecting %d statistics for %s (%s to %s)",
            len(consumption),
            statistic_id,
            consumption[0]["start"],
            consumption[-1]["start"],
        )
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Thames Water {readings.meter_id} consumption",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=VolumeConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfVolume.LITERS,
            ),
            consumption,
        )
        return consumption

    async def _async_write_cost_statistics(
        self, meter_id: str, consumption: list[StatisticData]
    ) -> None:
        """Write the money one meter's readings cost.

        The Energy dashboard builds no cost sensor for an external statistic,
        so the cost is written as an external statistic of its own for the
        water source to point at.

        Each row is priced at the rate in force on the row's own date.
        Readings arrive days late, so a window spanning a price change would
        otherwise take whichever rate the run happened to scrape. Only the
        current rate is published, so readings from before it took effect are
        left unpriced rather than valued at a rate that was not theirs.
        """
        tariff = self._tariff_coordinator.data
        if tariff is None or not consumption:
            return

        statistic_id = cost_statistic_id(meter_id)
        priced_through, running_total = await self._async_cost_watermark(statistic_id)
        rows, unpriced = price_readings(
            consumption, tariff, priced_through, running_total
        )

        if unpriced:
            _LOGGER.debug(
                "Left %d readings from before %s unpriced: only the current "
                "rate is published",
                unpriced,
                tariff.effective_date,
            )
        if not rows:
            return

        _LOGGER.debug(
            "Injecting %d cost statistics for %s (%s to %s)",
            len(rows),
            statistic_id,
            rows[0]["start"],
            rows[-1]["start"],
        )
        async_add_external_statistics(
            self.hass,
            StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Thames Water {meter_id} cost",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=None,
                unit_of_measurement="GBP",
            ),
            rows,
        )

    async def _async_cost_watermark(
        self, statistic_id: str
    ) -> tuple[datetime | None, float]:
        """Return how far cost has been priced, and the total to carry on from.

        Cost accumulates, unlike consumption, whose sum is the meter's own
        odometer. A reading already priced is therefore never priced again,
        even when the day it falls in is re-requested.
        """
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if not last_stat:
            return None, 0.0

        row = last_stat[statistic_id][0]
        return dt_util.utc_from_timestamp(row["start"]), row.get("sum") or 0.0


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
