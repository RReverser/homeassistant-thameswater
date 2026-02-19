"""Config flow for Thames Water integration."""

import voluptuous as vol

from homeassistant import config_entries

from .const import DEFAULT_UPDATE_INTERVAL_HOURS, DOMAIN
from .thameswaterclient import AuthenticationError, ThamesWater


class ThamesWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Thames Water."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the config flow."""
        self._credentials: dict = {}

    async def async_step_user(self, user_input=None):
        """Handle the initial step: collect credentials and account number."""
        errors = {}
        if user_input is not None:
            self._credentials = user_input
            try:
                meter_numbers = await self.hass.async_add_executor_job(
                    self._fetch_meter_numbers,
                    user_input["username"],
                    user_input["password"],
                    user_input["account_number"],
                )
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                if not meter_numbers:
                    errors["base"] = "no_meters"
                else:
                    self._meter_numbers = meter_numbers
                    return await self.async_step_meter()

        data_schema = vol.Schema(
            {
                vol.Required(
                    "username", description={"suggested_value": "email@example.com"}
                ): str,
                vol.Required("password"): str,
                vol.Required("account_number"): str,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_meter(self, user_input=None):
        """Handle the meter selection step."""
        errors = {}
        if user_input is not None:
            return self.async_create_entry(
                title="Thames Water",
                data={
                    **self._credentials,
                    "meter_id": user_input["meter_id"],
                    "update_interval_hours": user_input["update_interval_hours"],
                },
            )

        data_schema = vol.Schema(
            {
                vol.Required("meter_id"): vol.In(self._meter_numbers),
                vol.Required(
                    "update_interval_hours",
                    default=DEFAULT_UPDATE_INTERVAL_HOURS,
                ): vol.All(int, vol.Range(min=1)),
            }
        )

        return self.async_show_form(
            step_id="meter", data_schema=data_schema, errors=errors
        )

    @staticmethod
    def _fetch_meter_numbers(
        username: str, password: str, account_number: str
    ) -> list[str]:
        """Fetch available meter numbers (blocking, run in executor)."""
        client = ThamesWater(
            email=username,
            password=password,
            account_number=account_number,
        )
        return client.get_meter_numbers()
