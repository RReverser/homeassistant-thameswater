"""Tests for setting up, refreshing and unloading a Thames Water entry."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

from thameswaterapi import AuthenticationError, MalformedResponse, RateLimitError

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.core import HomeAssistant

from custom_components.thames_water.coordinator import (
    CONSUMPTION_STATISTIC_ID,
    COST_STATISTIC_ID,
    HOURLY_STATISTIC_ID,
)

from .conftest import ACCOUNT_NUMBER, METER_ID, hourly_lines, make_meter_usage


async def _setup(hass: HomeAssistant, config_entry) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


def _written(statistics: MagicMock, statistic_id: str) -> list[dict]:
    """Return the rows pushed for one statistic id."""
    rows: list[dict] = []
    for call in statistics.call_args_list:
        metadata, stats = call.args[1], call.args[2]
        if metadata["statistic_id"] == statistic_id:
            rows.extend(stats)
    return rows


async def test_entry_sets_up_and_creates_entities(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED

    consumption = hass.states.get("sensor.thames_water_sensor")
    assert consumption is not None
    assert consumption.attributes["device_class"] == "water"
    assert consumption.attributes["unit_of_measurement"] == "L"

    balance = hass.states.get("sensor.thames_water_outstanding_balance")
    assert balance is not None
    assert balance.state == "42.5"
    assert balance.attributes["current_balance"] == -15.0
    assert balance.attributes["is_in_credit"] is True

    assert hass.states.get("sensor.thames_water_unit_rate").state == "0.003"


async def test_one_authentication_per_refresh(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    # 1.2.2 built a client per sensor and per granularity: three logins.
    await _setup(hass, config_entry)

    assert client.authenticate.call_count == 1
    assert client.get_account.call_count == 1


async def test_every_day_is_asked_for_hourly(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    granularities = {
        call.kwargs.get("granularity", "H")
        for call in client.get_meter_usage.call_args_list
    }
    assert granularities == {"H"}
    # With no statistics yet the window is the initial backfill.
    assert client.get_meter_usage.call_count == 31
    # Nothing came back empty behind a day that had readings, so no daily
    # request was needed.
    client.get_meter_usage_lines.assert_not_called()


async def test_a_gap_behind_the_frontier_falls_back_to_daily(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    published = date.today() - timedelta(days=3)
    missing = date.today() - timedelta(days=10)

    def get_meter_usage(meter, start, end, granularity="H"):
        if start == missing or start > published:
            return make_meter_usage([])
        return make_meter_usage(hourly_lines(1000.0))

    client.get_meter_usage.side_effect = get_meter_usage

    await _setup(hass, config_entry)

    client.get_meter_usage_lines.assert_called_once()
    args = client.get_meter_usage_lines.call_args
    assert args.args[1] == missing
    assert args.kwargs["granularity"] == "D"


async def test_statistics_are_written(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    hourly = _written(statistics, HOURLY_STATISTIC_ID)
    assert len(hourly) == 28 * 24
    assert hourly[0]["sum"] == 1000.0

    consumption = _written(statistics, CONSUMPTION_STATISTIC_ID)
    assert len(consumption) == len(hourly)
    # The sum is the meter odometer, never a synthetic running total.
    assert consumption[0]["sum"] == 1000.0

    cost = _written(statistics, COST_STATISTIC_ID)
    assert len(cost) == len(consumption)
    # 3.0 GBP/m3 is 0.003 GBP/L, and every hour uses 10L.
    assert cost[0]["state"] == 0.03
    assert cost[1]["sum"] == 0.06


async def test_a_rejected_password_starts_reauth(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    client.authenticate.side_effect = AuthenticationError("Your password is incorrect")

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_a_rate_limit_does_not_start_reauth(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    client.authenticate.side_effect = RateLimitError(120)

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_a_malformed_response_does_not_start_reauth(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    response = MagicMock(status_code=403, headers={"content-type": "text/html"})
    response.text = "<html>Forbidden</html>"
    client.get_account.side_effect = MalformedResponse(response, "unexpected status")

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_unload(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert (
        hass.states.get("sensor.thames_water_outstanding_balance").state
        == "unavailable"
    )


async def test_reauth_updates_the_password(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    client.authenticate.side_effect = AuthenticationError("Your password is incorrect")
    await _setup(hass, config_entry)

    flow = hass.config_entries.flow.async_progress()[0]

    # Still the wrong password: the form comes back saying so.
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"password": "still-wrong"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    client.authenticate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"password": "correcthorse"}
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["password"] == "correcthorse"
    assert config_entry.data["account_number"] == ACCOUNT_NUMBER
    assert config_entry.data["meter_id"] == METER_ID
