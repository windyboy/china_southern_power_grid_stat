"""Tests for coordinator ordering and per-sensor metadata."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy

from custom_components.china_southern_power_grid_stat.const import (
    DOMAIN,
    SUFFIX_BAL,
    SUFFIX_CURRENT_LADDER_REMAINING_KWH,
    SUFFIX_CURRENT_LADDER_TARIFF,
    SUFFIX_LAST_MONTH_KWH,
    SUFFIX_THIS_MONTH_COST,
    SUFFIX_THIS_MONTH_KWH,
    SUFFIX_THIS_YEAR_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from custom_components.china_southern_power_grid_stat.sensor import (
    CSGCoordinator,
    CSGCostSensor,
    CSGEnergySensor,
)


def make_coordinator():
    """Return the minimal coordinator shape used by entity constructors."""
    return SimpleNamespace(context=None, data={}, _config_entry_id="entry-id")


@pytest.mark.parametrize(
    ("suffix", "state_class"),
    [
        (SUFFIX_THIS_MONTH_KWH, SensorStateClass.TOTAL_INCREASING),
        (SUFFIX_THIS_YEAR_KWH, SensorStateClass.TOTAL_INCREASING),
        (SUFFIX_YESTERDAY_KWH, None),
        (SUFFIX_LAST_MONTH_KWH, None),
    ],
)
def test_energy_metadata_matches_period_semantics(suffix, state_class):
    sensor = CSGEnergySensor(make_coordinator(), "account", suffix)

    assert sensor.device_class is SensorDeviceClass.ENERGY
    assert sensor.native_unit_of_measurement is UnitOfEnergy.KILO_WATT_HOUR
    assert sensor.state_class is state_class
    assert sensor.unique_id == f"{DOMAIN}.entry-id.account.{suffix}"


def test_remaining_energy_is_a_current_storage_measurement():
    sensor = CSGEnergySensor(
        make_coordinator(), "account", SUFFIX_CURRENT_LADDER_REMAINING_KWH
    )

    assert sensor.device_class is SensorDeviceClass.ENERGY_STORAGE
    assert sensor.state_class is SensorStateClass.MEASUREMENT


def test_cost_metadata_distinguishes_totals_balance_and_tariff():
    current_total = CSGCostSensor(
        make_coordinator(), "account", SUFFIX_THIS_MONTH_COST
    )
    balance = CSGCostSensor(make_coordinator(), "account", SUFFIX_BAL)
    tariff = CSGCostSensor(
        make_coordinator(), "account", SUFFIX_CURRENT_LADDER_TARIFF
    )

    assert current_total.device_class is SensorDeviceClass.MONETARY
    assert current_total.state_class is SensorStateClass.TOTAL_INCREASING
    assert balance.device_class is SensorDeviceClass.MONETARY
    assert balance.state_class is None
    assert tariff.device_class is None
    assert tariff.state_class is None
    assert tariff.native_unit_of_measurement == "CNY/kWh"


def test_device_identifier_is_scoped_to_config_entry():
    sensor = CSGEnergySensor(
        make_coordinator(), "account", SUFFIX_YESTERDAY_KWH
    )

    assert sensor.device_info["identifiers"] == {(DOMAIN, "entry-id:account")}


@pytest.mark.asyncio
async def test_this_month_failure_cannot_block_last_month_update():
    coordinator = object.__new__(CSGCoordinator)
    coordinator._if_update_last_month = False
    coordinator._gathered_data = {"account": {}}
    coordinator._async_update_bal_arr = AsyncMock()
    coordinator._async_update_yesterday_kwh = AsyncMock()
    coordinator._async_update_this_year_stats = AsyncMock()
    coordinator._async_update_last_year_stats = AsyncMock()
    coordinator._async_update_this_month_stats_and_ladder = AsyncMock(
        side_effect=RuntimeError("this-month failed")
    )
    coordinator._async_update_last_month_stats = AsyncMock()
    coordinator._update_latest_day = lambda _account: None
    account = SimpleNamespace(account_number="account")

    await asyncio.wait_for(coordinator._async_update_account_data(account), timeout=1)

    coordinator._async_update_last_month_stats.assert_awaited_once_with(account)
    assert coordinator._if_update_last_month is True
