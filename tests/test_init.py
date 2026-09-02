"""Tests for setting up, refreshing and unloading a Thames Water entry."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock

from thameswaterapi import AuthenticationError, MalformedResponse, RateLimitError

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.thames_water.const import DOMAIN
from custom_components.thames_water.coordinator import (
    INITIAL_HISTORY,
    LONDON_TZ,
    consumption_statistic_id,
    cost_statistic_id,
)

from .conftest import ACCOUNTS, METERS, PUBLICATION_LAG_DAYS, make_meter_usage

FIRST_ACCOUNT, SECOND_ACCOUNT = ACCOUNTS
FIRST_METER = METERS[FIRST_ACCOUNT][0]
SECOND_METER = METERS[SECOND_ACCOUNT][0]


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


def _state_of(hass: HomeAssistant, unique_id: str):
    """Return the state of the entity with ``unique_id``."""
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None
    return hass.states.get(entity_id)


async def test_every_account_and_meter_is_discovered(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    # Nothing was chosen during setup, so both accounts and the meter on each
    # have to turn up by themselves.
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    assert client.get_meter_usage.call_count == len(METERS)

    for meter_id in (FIRST_METER, SECOND_METER):
        assert _state_of(hass, f"{meter_id}_meter_reading") is not None


async def test_devices_are_named_by_address(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    registry = dr.async_get(hass)
    account_device = registry.async_get_device(
        identifiers={(DOMAIN, f"account_{FIRST_ACCOUNT}")}
    )
    assert account_device is not None
    assert account_device.name == ACCOUNTS[FIRST_ACCOUNT]

    meter_device = registry.async_get_device(
        identifiers={(DOMAIN, f"meter_{FIRST_METER}")}
    )
    assert meter_device is not None
    assert meter_device.name == f"Meter {FIRST_METER}"
    # The meter hangs off the account whose address it sits at.
    assert meter_device.via_device_id == account_device.id


async def test_one_authentication_per_refresh(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    assert client.authenticate.call_count == 1
    # One account lookup each; assigning the account re-scopes the session.
    assert client.get_account.call_count == len(ACCOUNTS)


async def test_one_hourly_request_per_meter(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    for call in client.get_meter_usage.call_args_list:
        assert call.kwargs["granularity"] == "H"
        # With no statistics yet the window is the initial backfill, asked
        # for in one request rather than one per day.
        assert call.args[1] == date.today() - INITIAL_HISTORY
        assert call.args[2] == date.today()


async def test_statistics_are_written_per_meter(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    for meter_id in (FIRST_METER, SECOND_METER):
        consumption = _written(statistics, consumption_statistic_id(meter_id))
        # 30 days of backfill, of which the last three are not published yet.
        assert len(consumption) == 28 * 24
        # The sum is the meter odometer, never a synthetic running total.
        assert consumption[0]["sum"] == 1000.0
        assert consumption[0]["start"] == datetime.combine(
            date.today() - INITIAL_HISTORY, time.min, tzinfo=LONDON_TZ
        )

        cost = _written(statistics, cost_statistic_id(meter_id))
        assert len(cost) == len(consumption)
        # 3.0 GBP/m3 is 0.003 GBP/L, and every hour uses 10L.
        assert cost[0]["state"] == 0.03
        assert cost[1]["sum"] == 0.06


async def test_a_short_response_is_taken_as_it_comes(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    written = _written(statistics, consumption_statistic_id(FIRST_METER))
    newest = max(row["start"] for row in written)
    assert newest.date() == date.today() - timedelta(days=PUBLICATION_LAG_DAYS)


async def test_an_empty_response_writes_nothing(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    client.get_meter_usage.side_effect = None
    client.get_meter_usage.return_value = make_meter_usage([])

    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED
    statistics.assert_not_called()


async def test_the_meter_reading_records_no_history(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    # The newest published reading is days old, so the recorder would
    # timestamp it wrongly. The history is in the external statistic.
    await _setup(hass, config_entry)

    reading = _state_of(hass, f"{FIRST_METER}_meter_reading")
    assert "state_class" not in reading.attributes


async def test_the_last_reading_is_a_diagnostic_timestamp(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    state = _state_of(hass, f"{FIRST_METER}_last_reading")
    assert state.attributes["device_class"] == "timestamp"
    entry = er.async_get(hass).async_get(state.entity_id)
    assert entry.entity_category is er.EntityCategory.DIAGNOSTIC


async def test_balance_is_exposed_per_account(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    for account_number in ACCOUNTS:
        state = _state_of(hass, f"{account_number}_outstanding_balance")
        assert state.state == "42.5"
        assert state.attributes["current_balance"] == -15.0
        assert state.attributes["is_in_credit"] is True


async def test_a_meter_added_later_appears_without_reconfiguring(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    await _setup(hass, config_entry)

    new_meter = "311999999"
    METERS[FIRST_ACCOUNT].append(new_meter)
    try:
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        assert _state_of(hass, f"{new_meter}_meter_reading") is not None
    finally:
        METERS[FIRST_ACCOUNT].remove(new_meter)


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
    assert _state_of(hass, f"{FIRST_METER}_meter_reading").state == "unavailable"
