from __future__ import annotations

import voluptuous as vol

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_MAC_ADDRESS, CONF_TIME_OFFSET, DOMAIN


class MedisanaConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        await self.async_set_unique_id(discovery_info.address.upper())
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> FlowResult:
        default_mac = (
            self._discovery_info.address if self._discovery_info else ""
        )

        if user_input is not None:
            mac = user_input[CONF_MAC_ADDRESS].upper()
            await self.async_set_unique_id(mac)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Medisana BS444 ({mac})",
                data={
                    CONF_MAC_ADDRESS: mac,
                    CONF_TIME_OFFSET: user_input[CONF_TIME_OFFSET],
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC_ADDRESS, default=default_mac): str,
                    vol.Required(CONF_TIME_OFFSET, default=True): bool,
                }
            ),
        )
