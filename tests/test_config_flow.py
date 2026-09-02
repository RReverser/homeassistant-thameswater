"""Tests for the Thames Water config flow."""

from __future__ import annotations

from thameswaterapi import AuthenticationError

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.thames_water.const import DOMAIN

from .conftest import PASSWORD, USERNAME


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


async def test_credentials_are_all_it_asks_for(
    integration, hass: HomeAssistant, client
) -> None:
    # Accounts and meters are discovered per refresh, so the flow has one
    # step and no picking.
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert set(result["data_schema"].schema) == {CONF_USERNAME, CONF_PASSWORD}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"] == {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


async def test_a_rejected_password_is_reported_on_the_form(
    integration, hass: HomeAssistant, client
) -> None:
    client.authenticate.side_effect = AuthenticationError("Your password is incorrect")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    # And the form takes a correct password without starting over.
    client.authenticate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_an_unreachable_server_is_reported_on_the_form(
    integration, hass: HomeAssistant, client
) -> None:
    client.authenticate.side_effect = TimeoutError

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_same_login_cannot_be_added_twice(
    integration, hass: HomeAssistant, config_entry, client
) -> None:
    config_entry.add_to_hass(hass)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME.upper(), CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_the_password(
    integration, hass: HomeAssistant, config_entry, client, statistics
) -> None:
    client.authenticate.side_effect = AuthenticationError("Your password is incorrect")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    flow = hass.config_entries.flow.async_progress()[0]
    assert flow["context"]["source"] == "reauth"

    # Still the wrong password: the form comes back saying so.
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_PASSWORD: "still-wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    client.authenticate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_PASSWORD: "correcthorse"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_PASSWORD] == "correcthorse"
    assert config_entry.data[CONF_USERNAME] == USERNAME
