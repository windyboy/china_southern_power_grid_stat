"""Configuration helpers for the China Southern Power Grid integration."""

from __future__ import annotations

import socket

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_IP_FAMILY,
    CONF_SETTINGS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_IP_FAMILY,
    DEFAULT_UPDATE_INTERVAL,
    IP_FAMILY_AUTO,
    IP_FAMILY_IPV4,
    IP_FAMILY_IPV6,
)

IP_FAMILY_TO_SOCKET = {
    IP_FAMILY_IPV4: socket.AF_INET,
    IP_FAMILY_AUTO: socket.AF_UNSPEC,
    IP_FAMILY_IPV6: socket.AF_INET6,
}


def get_configured_ip_family(entry: ConfigEntry | None) -> str:
    """Return the configured address-family mode, defaulting safely to IPv4."""
    if entry is None:
        return DEFAULT_IP_FAMILY
    mode = entry.options.get(CONF_IP_FAMILY, DEFAULT_IP_FAMILY)
    if mode not in IP_FAMILY_TO_SOCKET:
        return DEFAULT_IP_FAMILY
    return mode


def get_configured_update_interval(entry: ConfigEntry) -> int:
    """Return the options value, falling back to the legacy data location."""
    legacy_settings = entry.data.get(CONF_SETTINGS, {})
    return entry.options.get(
        CONF_UPDATE_INTERVAL,
        legacy_settings.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
    )


def async_get_csg_clientsession(
    hass: HomeAssistant, entry: ConfigEntry | None = None
) -> aiohttp.ClientSession:
    """Return the HA-managed session for the configured CSG address family."""
    family = IP_FAMILY_TO_SOCKET[get_configured_ip_family(entry)]
    return async_get_clientsession(hass, family=family)
