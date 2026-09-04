"""Config flow for Thames Water integration."""

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.util import dt as dt_util

from .const import DEFAULT_HISTORY_DAYS, DEFAULT_UPDATE_INTERVAL_HOURS, DOMAIN
from .coordinator import LONDON_TZ, write_consumption_statistics
from thameswaterapi import AuthenticationError, Line, ThamesWater


class ThamesWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Thames Water."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._credentials: dict = {}
        self._client: ThamesWater | None = None

    async def async_step_user(self, user_input=None):
        """Handle the initial step: collect credentials."""
        errors = {}
        if user_input is not None:
            self._credentials = user_input
            try:
                self._client = await self.hass.async_add_executor_job(
                    self._authenticate,
                    user_input["username"],
                    user_input["password"],
                )
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_account()

        data_schema = vol.Schema(
            {
                vol.Required(
                    "username", description={"suggested_value": "email@example.com"}
                ): str,
                vol.Required("password"): str,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        """Handle a password rejected by Thames Water."""
        self._credentials = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Collect a new password for the entry being reauthenticated."""
        errors = {}
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    self._authenticate,
                    self._credentials["username"],
                    user_input["password"],
                )
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                entry = self.hass.config_entries.async_get_entry(
                    self.context["entry_id"]
                )
                assert entry is not None
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, "password": user_input["password"]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required("password"): str}),
            description_placeholders={"username": self._credentials["username"]},
            errors=errors,
        )

    async def async_step_account(self, user_input=None):
        """Handle the account selection step."""
        assert self._client is not None

        account_numbers = self._client.get_account_numbers()
        if len(account_numbers) <= 1:
            self._credentials["account_number"] = str(self._client.account_number)
            return await self.async_step_meter()

        if user_input is not None:
            self._credentials["account_number"] = user_input["account_number"]
            self._client.account_number = int(user_input["account_number"])
            await self.hass.async_add_executor_job(self._client._visit_meter_page)
            return await self.async_step_meter()

        account_options = {str(n): str(n) for n in account_numbers}
        data_schema = vol.Schema(
            {
                vol.Required("account_number"): vol.In(account_options),
            }
        )

        return self.async_show_form(step_id="account", data_schema=data_schema)

    async def async_step_meter(self, user_input=None):
        """Handle the meter selection step."""
        assert self._client is not None
        errors = {}

        if user_input is not None:
            start_day = dt_util.now(LONDON_TZ).date() - timedelta(
                days=user_input["history_days"]
            )
            try:
                lines = await self.hass.async_add_executor_job(
                    self._fetch_history, user_input["meter_id"], start_day
                )
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                write_consumption_statistics(self.hass, start_day, lines)
                return self.async_create_entry(
                    title="Thames Water",
                    data={
                        **self._credentials,
                        "meter_id": user_input["meter_id"],
                        "update_interval_hours": user_input["update_interval_hours"],
                    },
                )

        meter_numbers = await self.hass.async_add_executor_job(
            self._client.get_meter_numbers
        )
        if not meter_numbers:
            errors["base"] = "no_meters"
            return self.async_show_form(
                step_id="meter", data_schema=vol.Schema({}), errors=errors
            )

        data_schema = vol.Schema(
            {
                vol.Required("meter_id"): vol.In(meter_numbers),
                vol.Required(
                    "update_interval_hours",
                    default=DEFAULT_UPDATE_INTERVAL_HOURS,
                ): vol.All(int, vol.Range(min=1)),
                # Width costs nothing: however many days are asked for, the
                # readings come back in one request.
                vol.Required(
                    "history_days",
                    default=DEFAULT_HISTORY_DAYS,
                ): vol.All(int, vol.Range(min=1)),
            }
        )

        return self.async_show_form(
            step_id="meter", data_schema=data_schema, errors=errors
        )

    def _fetch_history(self, meter_id: str, start_day: date) -> list[Line]:
        """Fetch the readings to seed the statistics with (blocking).

        One request covers the whole window, so the depth chosen above makes
        no difference to how long this step takes.
        """
        assert self._client is not None
        usage = self._client.get_meter_usage(
            meter_id, start_day, dt_util.now(LONDON_TZ).date(), granularity="H"
        )
        return usage.Lines

    @staticmethod
    def _authenticate(username: str, password: str) -> ThamesWater:
        """Authenticate with Thames Water (blocking, run in executor)."""
        return ThamesWater(email=username, password=password)
