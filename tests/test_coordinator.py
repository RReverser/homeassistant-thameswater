"""Tests for the Thames Water coordinator's statistics and fetch planning."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from homeassistant.components.recorder.models import StatisticData
from thameswaterapi import Line, Tariff

from custom_components.thames_water.coordinator import (
    consumption_statistic_id,
    cost_statistic_id,
    generate_hourly_statistics,
    price_readings,
)

LONDON_TZ = ZoneInfo("Europe/London")


def _make_line(usage: float, read: float, label: str = "") -> Line:
    return Line(
        Label=label,
        Usage=usage,
        Read=read,
        IsEstimated=False,
        MeterSerialNumberHis="",
    )


class TestGenerateHourlyStatistics:
    def _day(self, skip=()):
        return [
            _make_line(10.0, 100.0 + hour * 10, f"{hour}:00")
            for hour in range(24)
            if hour not in skip
        ]

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

    def test_one_day(self) -> None:
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

    def test_a_window_spans_as_many_days_as_it_carries(self) -> None:
        stats = generate_hourly_statistics(
            date(2026, 2, 10), self._day() + self._day() + self._day()
        )
        assert len(stats) == 72
        assert stats[23]["start"] == datetime(2026, 2, 10, 23, 0, tzinfo=LONDON_TZ)
        assert stats[24]["start"] == datetime(2026, 2, 11, 0, 0, tzinfo=LONDON_TZ)
        assert stats[-1]["start"] == datetime(2026, 2, 12, 23, 0, tzinfo=LONDON_TZ)

    def test_a_short_day_does_not_drag_the_next_one_back(self) -> None:
        # 29 March 2026 is 23 hours long: there is no 1:00 local.
        stats = generate_hourly_statistics(
            date(2026, 3, 28), self._day() + self._day(skip={1}) + self._day()
        )
        assert len(stats) == 71
        assert stats[-1]["start"] == datetime(2026, 3, 30, 23, 0, tzinfo=LONDON_TZ)

    def test_a_missing_hour_does_not_shift_the_rest(self) -> None:
        stats = generate_hourly_statistics(
            date(2024, 1, 1),
            [_make_line(10.0, 100.0, "0:00"), _make_line(5.0, 125.0, "14:00")],
        )
        assert [stat["start"] for stat in stats] == [
            datetime(2024, 1, 1, 0, 0, tzinfo=LONDON_TZ),
            datetime(2024, 1, 1, 14, 0, tzinfo=LONDON_TZ),
        ]

    def test_usage_values_are_truncated_to_int(self) -> None:
        stats = generate_hourly_statistics(
            date(2024, 1, 1), [_make_line(99.7, 1000.3, "0:00")]
        )
        assert stats[0]["state"] == 99
        assert stats[0]["sum"] == 1000

    def test_timestamps_are_timezone_aware(self) -> None:
        stats = generate_hourly_statistics(
            date(2024, 1, 1), [_make_line(10.0, 100.0, "0:00")]
        )
        assert stats[0]["start"].tzinfo is not None


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


class TestStatisticIds:
    def test_one_series_per_meter(self) -> None:
        assert consumption_statistic_id("311228415") == (
            "thames_water:311228415_consumption"
        )
        assert cost_statistic_id("311228415") == "thames_water:311228415_cost"

    def test_a_serial_is_slugified(self) -> None:
        # valid_statistic_id wants both halves to be slugs.
        assert consumption_statistic_id("31-12/284 15") == (
            "thames_water:31_12_284_15_consumption"
        )
