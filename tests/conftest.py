"""Fixtures for the Thames Water integration tests."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from thameswaterapi import Account, Line, MeterUsage, Tariff

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thames_water.const import DOMAIN

METER_ID = "311307160"
ACCOUNT_NUMBER = "900000000000"

TARIFF = Tariff(
    clean_water_rate_per_m3=2.0,
    wastewater_rate_per_m3=1.0,
    water_fixed_per_year=66.87,
    wastewater_fixed_per_year=128.13,
    effective_date=date(2020, 1, 1),
)

ACCOUNT = Account(
    contractAccountNumber=ACCOUNT_NUMBER,
    paymentDueAmount=42.5,
    currentBalance=-15.0,
    isInCredit=True,
)


@pytest.fixture
def integration(recorder_mock, enable_custom_integrations):
    """Set up what a config entry needs, in the order the harness wants it.

    The recorder has to be mocked before Home Assistant itself is built, and
    enable_custom_integrations builds it, so the two are ordered here rather
    than in every test signature.
    """
    return


def make_meter_usage(lines: list[Line]) -> MeterUsage:
    """Build a MeterUsage response carrying ``lines``."""
    return MeterUsage(
        IsError=False,
        IsDataAvailable=bool(lines),
        IsConsumptionAvailable=bool(lines),
        TargetUsage=0.0,
        AverageUsage=0.0,
        ActualUsage=0.0,
        MyUsage=None,
        AverageUsagePerPerson=0.0,
        IsMO365Customer=False,
        IsMOPartialCustomer=False,
        IsMOCompleteCustomer=False,
        IsExtraMonthConsumptionMessage=False,
        Lines=lines,
    )


def hourly_lines(read: float, hours: int = 24) -> list[Line]:
    """Build a day of hourly lines whose odometer climbs by 10L an hour."""
    return [
        Line(
            Label=f"{hour}:00",
            Usage=10.0,
            Read=read + hour * 10,
            IsEstimated=False,
            MeterSerialNumberHis=METER_ID,
        )
        for hour in range(hours)
    ]


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured Thames Water entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Thames Water",
        data={
            "username": "user@example.com",
            "password": "hunter2",
            "account_number": ACCOUNT_NUMBER,
            "meter_id": METER_ID,
            "update_interval_hours": 12,
        },
    )


@pytest.fixture
def client() -> Generator[MagicMock]:
    """Patch the library client, answering with days up to the publication lag."""
    with (
        patch(
            "custom_components.thames_water.coordinator.ThamesWater", autospec=True
        ) as client_class,
        patch(
            "custom_components.thames_water.config_flow.ThamesWater",
            new=client_class,
        ),
    ):
        client = client_class.return_value
        published = date.today() - timedelta(days=3)

        def get_meter_usage(meter, start, end, granularity="H"):
            # Only days up to the publication lag have readings.
            return make_meter_usage(hourly_lines(1000.0) if start <= published else [])

        client.get_meter_usage.side_effect = get_meter_usage
        client.get_meter_usage_lines.return_value = []
        client.get_account.return_value = ACCOUNT
        yield client


@pytest.fixture(autouse=True)
def tariff() -> Generator[MagicMock]:
    """Patch the tariff scrape, which is a separate unauthenticated page."""
    with patch(
        "custom_components.thames_water.coordinator.get_tariff", return_value=TARIFF
    ) as get:
        yield get


@pytest.fixture
def statistics() -> Generator[MagicMock]:
    """Capture what is pushed to the recorder."""
    with patch(
        "custom_components.thames_water.coordinator.async_add_external_statistics"
    ) as add:
        yield add
