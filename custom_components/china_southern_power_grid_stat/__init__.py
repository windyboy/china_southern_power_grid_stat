"""The China Southern Power Grid Statistics integration."""
from __future__ import annotations

import copy
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry
from homeassistant.helpers.device_registry import DeviceEntry

from .config import async_get_csg_clientsession
from .const import (
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_LOGIN_TYPE,
    CONF_UPDATED_AT,
    DOMAIN,
)
from .csg_client import (
    CSGClient,
    CSGTransportError,
)

PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


def _create_entry_update_listener(
    initial_data: Mapping[str, Any],
    initial_options: Mapping[str, Any],
) -> Callable[[HomeAssistant, ConfigEntry], Awaitable[None]]:
    """Reload when either config-entry data or options change."""
    previous_data = copy.deepcopy(dict(initial_data))
    previous_options = copy.deepcopy(dict(initial_options))

    async def _async_reload_on_options_update(
        hass: HomeAssistant, updated_entry: ConfigEntry
    ) -> None:
        nonlocal previous_data, previous_options
        if (
            updated_entry.data == previous_data
            and updated_entry.options == previous_options
        ):
            return
        previous_data = copy.deepcopy(dict(updated_entry.data))
        previous_options = copy.deepcopy(dict(updated_entry.options))
        await hass.config_entries.async_reload(updated_entry.entry_id)

    return _async_reload_on_options_update


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up China Southern Power Grid Statistics from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # validate session, re-authenticate if needed
    session = async_get_csg_clientsession(hass, entry)
    client = CSGClient.load(
        {
            CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
        },
        session,
    )
    try:
        login_ok = await client.verify_login()
    except CSGTransportError as err:
        raise ConfigEntryNotReady(f"CSG network unreachable: {err}") from err
    if not login_ok:
        raise ConfigEntryAuthFailed("Login expired")

    hass.data[DOMAIN][entry.entry_id] = {}

    # Optional: remove legacy password from stored config if present
    if CONF_PASSWORD in entry.data:
        new_data = copy.deepcopy(dict(entry.data))
        new_data.pop(CONF_PASSWORD, None)
        hass.config_entries.async_update_entry(entry, data=new_data)

    entry.async_on_unload(
        entry.add_update_listener(
            _create_entry_update_listener(entry.data, entry.options)
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading entry: %s", entry.title)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug("Unload platforms for entry: %s, success: %s", entry.title, unload_ok)
    hass.data[DOMAIN].pop(entry.entry_id)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove device"""
    if not device_entry.identifiers:
        _LOGGER.warning("Cannot remove device with no identifiers: %s", device_entry)
        return False
    _LOGGER.info("Removing device %s", device_entry.name)
    identifier = next(
        (value for domain, value in device_entry.identifiers if domain == DOMAIN), None
    )
    if identifier is None:
        _LOGGER.warning("Cannot remove device without a %s identifier", DOMAIN)
        return False
    entry_prefix = f"{config_entry.entry_id}:"
    account_num = identifier.removeprefix(entry_prefix)

    # remove entities
    entity_reg = entity_registry.async_get(hass)
    entities = {
        ent.unique_id: ent.entity_id
        for ent in entity_registry.async_entries_for_config_entry(
            entity_reg, config_entry.entry_id
        )
        if ent.unique_id.startswith(
            (
                f"{DOMAIN}.{config_entry.entry_id}.{account_num}.",
                f"{DOMAIN}.{account_num}.",
            )
        )
    }
    for entity_id in entities.values():
        entity_reg.async_remove(entity_id)

    # update config entry (only if account was in config)
    new_data = copy.deepcopy(dict(config_entry.data))
    if new_data[CONF_ELE_ACCOUNTS].pop(account_num, None) is None:
        _LOGGER.debug("Account %s was not in config, skip update", account_num)
        return True
    new_data[CONF_UPDATED_AT] = str(int(time.time() * 1000))
    hass.config_entries.async_update_entry(
        config_entry,
        data=new_data,
    )
    _LOGGER.info(
        "Removed ele account from %s: %s",
        config_entry.data[CONF_USERNAME],
        account_num,
    )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    _LOGGER.info("Removing entry: account %s", entry.data[CONF_USERNAME])

    # logout
    session = async_get_csg_clientsession(hass, entry)
    client = CSGClient.load(
        {
            CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
        },
        session,
    )
    if await client.verify_login():
        await client.logout(entry.data[CONF_LOGIN_TYPE])
        _LOGGER.info("CSG account %s logged out", entry.data[CONF_USERNAME])
