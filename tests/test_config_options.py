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
    async_get_csg_clientsession_for_family,
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
    CONF_GENERAL_ERROR,
    CONF_IP_FAMILY,
    CONF_LOGIN_TYPE,
    CONF_SETTINGS,
    CONF_UPDATE_INTERVAL,
    ERROR_CANNOT_CONNECT,
    IP_FAMILY_AUTO,
    IP_FAMILY_IPV4,
    IP_FAMILY_IPV6,
)
from custom_components.china_southern_power_grid_stat.csg_client import (
    CSGTransportError,
    LoginType,
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
def test_missing_or_invalid_family_safely_defaults_to_auto(entry):
    assert get_configured_ip_family(entry) == IP_FAMILY_AUTO


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
    assert defaults[CONF_IP_FAMILY] == IP_FAMILY_AUTO

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


@pytest.mark.parametrize(
    ("mode", "expected_family"),
    [
        (IP_FAMILY_IPV4, socket.AF_INET),
        (IP_FAMILY_AUTO, socket.AF_UNSPEC),
        (IP_FAMILY_IPV6, socket.AF_INET6),
    ],
)
def test_session_for_family_maps_address_family_modes(monkeypatch, mode, expected_family):
    captured = {}

    def fake_get_session(hass, *, family):
        captured["family"] = family
        return object()

    monkeypatch.setattr(
        "custom_components.china_southern_power_grid_stat.config.async_get_clientsession",
        fake_get_session,
    )

    async_get_csg_clientsession_for_family(object(), mode)

    assert captured == {"family": expected_family}


def test_session_for_family_invalid_falls_back_to_default(monkeypatch):
    captured = {}

    def fake_get_session(hass, *, family):
        captured["family"] = family
        return object()

    monkeypatch.setattr(
        "custom_components.china_southern_power_grid_stat.config.async_get_clientsession",
        fake_get_session,
    )

    async_get_csg_clientsession_for_family(object(), "bogus")

    assert captured == {"family": socket.AF_UNSPEC}


def test_config_flow_default_ip_family_new_entry_is_auto():
    flow = object.__new__(CSGConfigFlow)
    flow._reauth_entry = None

    assert flow._default_ip_family() == IP_FAMILY_AUTO


def test_config_flow_default_ip_family_reauth_prefills_entry_mode():
    flow = object.__new__(CSGConfigFlow)
    flow._reauth_entry = make_entry(options={CONF_IP_FAMILY: IP_FAMILY_IPV6})

    assert flow._default_ip_family() == IP_FAMILY_IPV6


def test_config_flow_get_ip_family_reads_context_selection():
    flow = object.__new__(CSGConfigFlow)
    flow._reauth_entry = None
    flow.context = {"user_data": {CONF_IP_FAMILY: IP_FAMILY_IPV6}}

    assert flow._get_ip_family() == IP_FAMILY_IPV6


def test_config_flow_get_ip_family_defaults_to_auto_without_selection():
    flow = object.__new__(CSGConfigFlow)
    flow._reauth_entry = None
    flow.context = {"user_data": {}}

    assert flow._get_ip_family() == IP_FAMILY_AUTO


def test_config_flow_new_client_uses_selected_family(monkeypatch):
    captured = {}
    fake_session = object()

    def fake_session_for_family(hass, ip_family):
        captured["ip_family"] = ip_family
        return fake_session

    monkeypatch.setattr(
        "custom_components.china_southern_power_grid_stat.config_flow"
        ".async_get_csg_clientsession_for_family",
        fake_session_for_family,
    )
    flow = object.__new__(CSGConfigFlow)
    flow._reauth_entry = None
    flow.context = {"user_data": {CONF_IP_FAMILY: IP_FAMILY_IPV6}}
    flow.hass = object()

    client = flow._new_client()

    assert captured == {"ip_family": IP_FAMILY_IPV6}
    assert client._session is fake_session


@pytest.mark.asyncio
async def test_qr_creation_transport_error_stays_in_flow():
    flow = object.__new__(CSGConfigFlow)
    flow._reauth_entry = None
    flow.context = {"user_data": {CONF_LOGIN_TYPE: LoginType.LOGIN_TYPE_WX_QR}}

    class FakeClient:
        async def api_create_login_qr_code(self, channel):
            raise CSGTransportError("boom")

    flow._new_client = lambda: FakeClient()

    captured = {}
    flow._show_qr_form = lambda login_type, errors=None: captured.update(
        login_type=login_type, errors=errors
    )

    await flow.async_step_qr_login(None)

    assert captured == {
        "login_type": LoginType.LOGIN_TYPE_WX_QR,
        "errors": {CONF_GENERAL_ERROR: ERROR_CANNOT_CONNECT},
    }


@pytest.mark.asyncio
async def test_qr_status_transport_error_stays_in_flow():
    flow = object.__new__(CSGConfigFlow)
    flow._reauth_entry = None
    flow.context = {
        "user_data": {
            CONF_LOGIN_TYPE: LoginType.LOGIN_TYPE_WX_QR,
            "login_id": "id",
            "image_link": "http://img",
        }
    }

    class FakeClient:
        async def api_get_qr_login_status(self, login_id):
            raise CSGTransportError("boom")

    flow._new_client = lambda: FakeClient()

    captured = {}
    flow._show_qr_form = lambda login_type, errors=None: captured.update(
        login_type=login_type, errors=errors
    )

    await flow.async_step_validate_qr_login(None)

    assert captured == {
        "login_type": LoginType.LOGIN_TYPE_WX_QR,
        "errors": {CONF_GENERAL_ERROR: ERROR_CANNOT_CONNECT},
    }


@pytest.mark.asyncio
async def test_create_entry_persists_selected_ip_family():
    flow = object.__new__(CSGConfigFlow)
    flow._reauth_entry = None
    flow.context = {"user_data": {CONF_IP_FAMILY: IP_FAMILY_IPV6}}

    captured = {}
    flow.async_create_entry = lambda title, data, options=None: captured.update(
        title=title, options=options
    )

    await flow.create_or_update_config_entry(
        "token", LoginType.LOGIN_TYPE_SMS, "", "13800000000"
    )

    assert captured["options"] == {CONF_IP_FAMILY: IP_FAMILY_IPV6}
