"""Tests for configurable CSG address-family behavior."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from homeassistant.const import CONF_USERNAME

from custom_components.china_southern_power_grid_stat import (
    _create_options_update_listener,
)
from custom_components.china_southern_power_grid_stat.config import (
    async_get_csg_clientsession,
    get_configured_ip_family,
    get_configured_update_interval,
)
from custom_components.china_southern_power_grid_stat.config_flow import (
    CSGConfigFlow,
    CSGOptionsFlowHandler,
)
from custom_components.china_southern_power_grid_stat.const import (
    CONF_ACCOUNT_NUMBER,
    CONF_ELE_ACCOUNTS,
    CONF_IP_FAMILY,
    CONF_SETTINGS,
    CONF_UPDATE_INTERVAL,
    IP_FAMILY_AUTO,
    IP_FAMILY_IPV4,
    IP_FAMILY_IPV6,
)


def make_entry(*, options=None, data=None, entry_id="entry-id"):
    """Create the small config-entry shape required by these tests."""
    return SimpleNamespace(
        options=options or {},
        data=data or {},
        entry_id=entry_id,
    )


@pytest.mark.parametrize(
    ("mode", "expected_family"),
    [
        (IP_FAMILY_IPV4, socket.AF_INET),
        (IP_FAMILY_AUTO, socket.AF_UNSPEC),
        (IP_FAMILY_IPV6, socket.AF_INET6),
    ],
)
def test_session_maps_all_address_family_modes(monkeypatch, mode, expected_family):
    captured = {}
    expected_session = object()

    def fake_get_session(hass, *, family):
        captured["hass"] = hass
        captured["family"] = family
        return expected_session

    monkeypatch.setattr(
        "custom_components.china_southern_power_grid_stat.config.async_get_clientsession",
        fake_get_session,
    )
    hass = object()
    entry = make_entry(options={CONF_IP_FAMILY: mode})

    session = async_get_csg_clientsession(hass, entry)

    assert session is expected_session
    assert captured == {"hass": hass, "family": expected_family}


@pytest.mark.parametrize(
    "entry",
    [
        None,
        make_entry(),
        make_entry(options={CONF_IP_FAMILY: "unexpected"}),
    ],
)
def test_missing_or_invalid_family_safely_defaults_to_ipv4(entry):
    assert get_configured_ip_family(entry) == IP_FAMILY_IPV4


def test_update_interval_prefers_options_and_supports_legacy_data():
    legacy = make_entry(data={CONF_SETTINGS: {CONF_UPDATE_INTERVAL: 300}})
    configured = make_entry(
        options={CONF_UPDATE_INTERVAL: 120},
        data={CONF_SETTINGS: {CONF_UPDATE_INTERVAL: 300}},
    )

    assert get_configured_update_interval(legacy) == 300
    assert get_configured_update_interval(configured) == 120


def test_config_flow_passes_entry_to_options_handler():
    entry = make_entry()

    handler = CSGConfigFlow.async_get_options_flow(entry)

    assert handler.config_entry is entry


@pytest.mark.asyncio
async def test_options_listener_reloads_once_only_when_options_change():
    class FakeConfigEntries:
        def __init__(self):
            self.reloads = []

        async def async_reload(self, entry_id):
            self.reloads.append(entry_id)

    config_entries = FakeConfigEntries()
    hass = SimpleNamespace(config_entries=config_entries)
    entry = make_entry(options={CONF_IP_FAMILY: IP_FAMILY_IPV4})
    listener = _create_options_update_listener(entry.options)

    await listener(hass, entry)
    entry.options = {CONF_IP_FAMILY: IP_FAMILY_AUTO}
    await listener(hass, entry)
    await listener(hass, entry)

    assert config_entries.reloads == [entry.entry_id]


@pytest.mark.asyncio
async def test_settings_flow_defaults_and_writes_standard_options():
    entry = make_entry(
        options={"preserved": True},
        data={CONF_SETTINGS: {CONF_UPDATE_INTERVAL: 300}},
    )
    flow = CSGOptionsFlowHandler(entry)

    form = await flow.async_step_settings()
    defaults = form["data_schema"]({})

    assert defaults[CONF_UPDATE_INTERVAL] == 300
    assert defaults[CONF_IP_FAMILY] == IP_FAMILY_IPV4

    result = await flow.async_step_settings(
        {CONF_UPDATE_INTERVAL: 600, CONF_IP_FAMILY: IP_FAMILY_IPV6}
    )

    assert result["data"] == {
        "preserved": True,
        CONF_UPDATE_INTERVAL: 600,
        CONF_IP_FAMILY: IP_FAMILY_IPV6,
    }


@pytest.mark.asyncio
async def test_add_account_flow_preserves_existing_options():
    class FakeConfigEntries:
        def async_entries(self, _domain):
            return [entry]

        def async_update_entry(self, _entry, *, data):
            _entry.data = data

        async def async_reload(self, _entry_id):
            return None

    entry = make_entry(
        options={CONF_IP_FAMILY: IP_FAMILY_IPV6, CONF_UPDATE_INTERVAL: 600},
        data={CONF_ELE_ACCOUNTS: {}, CONF_USERNAME: "user"},
    )
    account = SimpleNamespace(account_number="account", dump=lambda: {"id": 1})
    flow = CSGOptionsFlowHandler(entry)
    flow.hass = SimpleNamespace(config_entries=FakeConfigEntries())
    flow.all_electricity_accounts = [account]

    result = await flow.async_step_add_account(
        {CONF_ACCOUNT_NUMBER: account.account_number}
    )

    assert result["data"] == entry.options
