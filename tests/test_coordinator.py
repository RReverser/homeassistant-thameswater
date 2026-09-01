"""Tests for the Thames Water coordinator's statistics and fetch planning."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import StatisticData
from thameswaterapi import Line, Tariff

from custom_components.thames_water.coordinator import (
    days_in_range,
    generate_daily_statistics,
    generate_hourly_statistics,
    price_readings,
    wants_hourly,
)

LONDON_TZ = ZoneInfo("Europe/London")

# Daily labels carry no year, so the library infers one from today's date.
# January is unambiguous whenever the response is read.
YEAR = date.today().year


def _make_line(usage: float, read: float, label: str = "") -> Line:
    return Line(
        Label=label,
        Usage=usage,
        Read=read,
        IsEstimated=False,
        MeterSerialNumberHis="",
    )


class TestGenerateHourlyStatistics:
    def test_empty_lines(self) -> None:
        assert generate_hourly_statistics(date(2024, 1, 1), []) == []

    def test_single_line(self) -> None:
        stats = generate_hourly_statistics(
            date(2024, 1, 1), [_make_line(10.0, 100.0, "0:00")]
        )
        assert len(stats) == 1
        assert stats[0]["start"] == datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ)
        assert stats[0]["state"] == 10
        assert stats[0]["sum"] == 100

    def test_multiple_lines(self) -> None:
        stats = generate_hourly_statistics(
            date(2024, 1, 1),
            [
                _make_line(10.0, 100.0, "0:00"),
                _make_line(20.0, 120.0, "1:00"),
                _make_line(5.0, 125.0, "2:00"),
            ],
        )
        assert [stat["start"] for stat in stats] == [
            datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ),
            datetime(2024, 1, 1, 1, 0, tzinfo=LONDON_TZ),
            datetime(2024, 1, 1, 2, 0, tzinfo=LONDON_TZ),
        ]
        assert [stat["state"] for stat in stats] == [10, 20, 5]

    def test_a_missing_hour_does_not_shift_the_rest(self) -> None:
        stats = generate_hourly_statistics(
            date(2024, 1, 1),
            [_make_line(10.0, 100.0, "0:00"), _make_line(5.0, 125.0, "14:00")],
        )
        assert [stat["start"] for stat in stats] == [
            datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ),
            datetime(2024, 1, 1, 14, 0, tzinfo=LONDON_TZ),
        ]


class TestGenerateDailyStatistics:
    def test_empty_lines(self) -> None:
        assert generate_daily_statistics([]) == []

    def test_single_line(self) -> None:
        stats = generate_daily_statistics([_make_line(100.0, 1000.0, "1-January")])
        assert len(stats) == 1
        assert stats[0]["start"] == datetime(YEAR, 1, 1, 0, 0, tzinfo=LONDON_TZ)
        assert stats[0]["state"] == 100
        assert stats[0]["sum"] == 1000

    def test_multiple_lines(self) -> None:
        stats = generate_daily_statistics(
            [
                _make_line(100.0, 1000.0, "1-January"),
                _make_line(150.0, 1150.0, "2-January"),
                _make_line(80.0, 1230.0, "3-January"),
            ]
        )
        assert [stat["start"] for stat in stats] == [
            datetime(YEAR, 1, 1, 0, 0, tzinfo=LONDON_TZ),
            datetime(YEAR, 1, 2, 0, 0, tzinfo=LONDON_TZ),
            datetime(YEAR, 1, 3, 0, 0, tzinfo=LONDON_TZ),
        ]
        assert [stat["state"] for stat in stats] == [100, 150, 80]

    def test_a_missing_day_does_not_shift_the_rest(self) -> None:
        # 2 January is absent from the response; 3 January must stay on the
        # 3rd rather than sliding back into the gap.
        stats = generate_daily_statistics(
            [
                _make_line(100.0, 1000.0, "1-January"),
                _make_line(80.0, 1230.0, "3-January"),
            ]
        )
        assert [stat["start"] for stat in stats] == [
            datetime(YEAR, 1, 1, 0, 0, tzinfo=LONDON_TZ),
            datetime(YEAR, 1, 3, 0, 0, tzinfo=LONDON_TZ),
        ]

    def test_timestamps_are_timezone_aware(self) -> None:
        stats = generate_daily_statistics([_make_line(100.0, 1000.0, "1-January")])
        assert stats[0]["start"].tzinfo is not None

    def test_usage_values_are_truncated_to_int(self) -> None:
        stats = generate_daily_statistics([_make_line(99.7, 1000.3, "1-January")])
        assert stats[0]["state"] == 99
        assert stats[0]["sum"] == 1000


class TestFetchPlanning:
    def test_same_day_is_hourly(self) -> None:
        assert wants_hourly(date(2026, 3, 1), date(2026, 3, 1))

    def test_one_day_behind_is_hourly(self) -> None:
        # The steady state at a 12-hour interval: one new day per cycle.
        assert wants_hourly(date(2026, 3, 1), date(2026, 3, 2))

    def test_two_days_behind_is_daily(self) -> None:
        assert not wants_hourly(date(2026, 3, 1), date(2026, 3, 3))

    def test_a_backfill_is_daily(self) -> None:
        assert not wants_hourly(date(2025, 1, 1), date(2026, 3, 3))

    def test_days_in_range_is_inclusive(self) -> None:
        assert days_in_range(date(2026, 3, 1), date(2026, 3, 3)) == [
            date(2026, 3, 1),
            date(2026, 3, 2),
            date(2026, 3, 3),
        ]

    def test_days_in_range_of_one_day(self) -> None:
        assert days_in_range(date(2026, 3, 1), date(2026, 3, 1)) == [date(2026, 3, 1)]


TARIFF = Tariff(
    clean_water_rate_per_m3=2.0,
    wastewater_rate_per_m3=1.0,
    water_fixed_per_year=100.0,
    wastewater_fixed_per_year=200.0,
    effective_date=date(2026, 4, 1),
)


def _reading(day: date, usage: int) -> StatisticData:
    return StatisticData(
        start=datetime.combine(day, datetime.min.time(), tzinfo=LONDON_TZ),
        state=usage,
        sum=0,
    )


class TestPriceReadings:
    def test_cost_accumulates_across_readings(self) -> None:
        rows, unpriced = price_readings(
            [_reading(date(2026, 4, 1), 1000), _reading(date(2026, 4, 2), 500)],
            TARIFF,
            None,
            0.0,
        )
        assert unpriced == 0
        # 3.0 GBP/m3 is 0.003 GBP/L.
        assert [row["state"] for row in rows] == [3.0, 1.5]
        assert [row["sum"] for row in rows] == [3.0, 4.5]

    def test_continues_from_the_running_total(self) -> None:
        rows, _ = price_readings([_reading(date(2026, 4, 2), 1000)], TARIFF, None, 10.0)
        assert rows[0]["sum"] == 13.0

    def test_readings_already_priced_are_not_priced_again(self) -> None:
        # The day holding the watermark is re-requested every cycle; pricing
        # its readings twice would double the cost.
        priced_through = datetime.combine(
            date(2026, 4, 1), datetime.min.time(), tzinfo=LONDON_TZ
        )
        rows, _ = price_readings(
            [_reading(date(2026, 4, 1), 1000), _reading(date(2026, 4, 2), 1000)],
            TARIFF,
            priced_through,
            3.0,
        )
        assert len(rows) == 1
        assert rows[0]["start"].date() == date(2026, 4, 2)
        assert rows[0]["sum"] == 6.0

    def test_readings_from_before_the_rate_took_effect_are_left_unpriced(self) -> None:
        rows, unpriced = price_readings(
            [_reading(date(2026, 3, 31), 1000), _reading(date(2026, 4, 1), 1000)],
            TARIFF,
            None,
            0.0,
        )
        assert unpriced == 1
        assert [row["start"].date() for row in rows] == [date(2026, 4, 1)]

    def test_no_readings(self) -> None:
        assert price_readings([], TARIFF, None, 0.0) == ([], 0)
