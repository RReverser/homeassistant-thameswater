"""Fixtures for the Thames Water integration tests."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from thameswaterapi import Account, Address, Line, MeterUsage, Property, Tariff

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thames_water.const import DOMAIN

USERNAME = "user@example.com"
PASSWORD = "hunter2"

# Two contract accounts, one meter each, as on the account under test.
ACCOUNTS = {
    900062968442: "1, Example Street, London, AB1 2CD",
    900096294185: "2, Other Road, London, EF3 4GH",
}
METERS = {900062968442: ["311228415"], 900096294185: ["311307160"]}
METER_ID = "311228415"

# Readings are published about three days in arrears.
PUBLICATION_LAG_DAYS = 3

TARIFF = Tariff(
    clean_water_rate_per_m3=2.0,
    wastewater_rate_per_m3=1.0,
    water_fixed_per_year=66.87,
    wastewater_fixed_per_year=128.13,
    effective_date=date(2020, 1, 1),
)


def make_account(account_number: int) -> Account:
    """Build an account carrying the address its device is named after."""
    return Account(
        contractAccountNumber=str(account_number),
        paymentDueAmount=42.5,
        currentBalance=-15.0,
        isInCredit=True,
        property=Property(
            propertyId="1",
            meterType=2,
            address=Address(
                addressLine1="1",
                addressLine2="Example Street",
                town="London",
                administrativeArea="",
                country="Gb",
                postcode="AB1 2CD",
                fullAddress=ACCOUNTS[account_number],
            ),
        ),
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


def hourly_lines(days: int, read: float = 1000.0) -> list[Line]:
    """Build whole days of hourly lines, the odometer climbing 10L an hour."""
    return [
        Line(
            Label=f"{hour}:00",
            Usage=10.0,
            Read=read + (day * 24 + hour) * 10,
            IsEstimated=False,
            MeterSerialNumberHis=METER_ID,
        )
        for day in range(days)
        for hour in range(24)
    ]


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured Thames Water entry: credentials and nothing else."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=USERNAME,
        data={CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD},
    )


@pytest.fixture
def client() -> Generator[MagicMock]:
    """Patch the library client, serving two accounts of one meter each."""
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
        client.account_number = next(iter(ACCOUNTS))

        client.get_account_numbers.return_value = list(ACCOUNTS)
        client.get_account.side_effect = lambda: make_account(client.account_number)
        client.get_meter_numbers.side_effect = lambda: METERS[client.account_number]

        def get_meter_usage(meter, start, end, granularity="H"):
            # The response is truncated on a whole-day boundary at the
            # publication lag, never padded out to the end of the window.
            published = date.today() - timedelta(days=PUBLICATION_LAG_DAYS)
            return make_meter_usage(hourly_lines((published - start).days + 1))

        client.get_meter_usage.side_effect = get_meter_usage
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
