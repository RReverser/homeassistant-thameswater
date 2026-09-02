"""Config flow for Thames Water integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from thameswaterapi import AuthenticationError, ThamesWater

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


class ThamesWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Thames Water."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the only step there is: the credentials.

        Contract accounts and meters are discovered on every refresh rather
        than chosen once here, so a meter added to the account later shows up
        on its own.
        """
        errors = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME]
            await self.async_set_unique_id(username.casefold())
            self._abort_if_unique_id_configured()

            errors = await self._async_try_credentials(
                username, user_input[CONF_PASSWORD]
            )
            if not errors:
                return self.async_create_entry(title=username, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        """Handle a password rejected by Thames Water."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Collect a new password for the entry being reauthenticated."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None

        errors = {}
        if user_input is not None:
            errors = await self._async_try_credentials(
                entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **user_input}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def _async_try_credentials(self, username: str, password: str) -> dict:
        """Return the form errors from signing in, empty if it worked."""
        try:
            await self.hass.async_add_executor_job(
                self._authenticate, username, password
            )
        except AuthenticationError:
            return {"base": "invalid_auth"}
        except Exception:  # noqa: BLE001
            return {"base": "cannot_connect"}
        return {}

    @staticmethod
    def _authenticate(username: str, password: str) -> None:
        """Sign in to Thames Water (blocking, run in executor)."""
        ThamesWater(email=username, password=password).authenticate()
