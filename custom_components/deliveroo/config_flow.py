"""Config flow for Deliveroo."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_client
from .api import (
    MARKETS,
    DeliverooAccount,
    DeliverooAuthError,
    DeliverooBlockedError,
    DeliverooError,
)
from .const import (
    CONF_ACTIVE_INTERVAL,
    CONF_IDLE_INTERVAL,
    CONF_MARKET,
    CONF_TOKEN,
    DEFAULT_ACTIVE_INTERVAL,
    DEFAULT_IDLE_INTERVAL,
    DOMAIN,
    MAX_ACTIVE_INTERVAL,
    MAX_IDLE_INTERVAL,
    MIN_ACTIVE_INTERVAL,
    MIN_IDLE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class DeliverooConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Deliveroo."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DeliverooOptionsFlow:
        """Return the options flow (polling intervals)."""
        return DeliverooOptionsFlow()

    async def _async_validate(
        self, market: str, token: str
    ) -> tuple[DeliverooAccount | None, str | None, dict[str, str]]:
        """Validate the cookie. Returns (account, possibly-rotated token, errors)."""
        client, session = create_client(self.hass, market, token, auto_cleanup=False)
        try:
            account = await client.async_get_account()
        except DeliverooAuthError:
            return None, None, {"base": "invalid_auth"}
        except DeliverooBlockedError:
            return None, None, {"base": "blocked"}
        except DeliverooError:
            return None, None, {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error validating Deliveroo credentials")
            return None, None, {"base": "unknown"}
        finally:
            session.detach()
        return account, client.token, {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            market = user_input[CONF_MARKET]
            account, token, errors = await self._async_validate(
                market, user_input[CONF_TOKEN]
            )
            if account is not None and token is not None:
                await self.async_set_unique_id(f"{market}_{account.customer_id}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Deliveroo ({account.name or account.customer_id})",
                    data={CONF_MARKET: market, CONF_TOKEN: token},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_MARKET, default="it"): SelectSelector(
                    SelectSelectorConfig(
                        options=list(MARKETS),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_MARKET,
                    )
                ),
                vol.Required(CONF_TOKEN): TOKEN_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth when the session cookie expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh session cookie."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            market = entry.data[CONF_MARKET]
            account, token, errors = await self._async_validate(
                market, user_input[CONF_TOKEN]
            )
            if account is not None and token is not None:
                await self.async_set_unique_id(f"{market}_{account.customer_id}")
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: token}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR}),
            errors=errors,
        )


def _seconds_selector(minimum: int, maximum: int, step: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement="s",
            mode=NumberSelectorMode.BOX,
        )
    )


class DeliverooOptionsFlow(OptionsFlowWithReload):
    """Let the user tune the polling intervals; the entry reloads on save."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_IDLE_INTERVAL: int(user_input[CONF_IDLE_INTERVAL]),
                    CONF_ACTIVE_INTERVAL: int(user_input[CONF_ACTIVE_INTERVAL]),
                }
            )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_IDLE_INTERVAL,
                    default=options.get(CONF_IDLE_INTERVAL, DEFAULT_IDLE_INTERVAL),
                ): _seconds_selector(MIN_IDLE_INTERVAL, MAX_IDLE_INTERVAL, 30),
                vol.Required(
                    CONF_ACTIVE_INTERVAL,
                    default=options.get(CONF_ACTIVE_INTERVAL, DEFAULT_ACTIVE_INTERVAL),
                ): _seconds_selector(MIN_ACTIVE_INTERVAL, MAX_ACTIVE_INTERVAL, 5),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
