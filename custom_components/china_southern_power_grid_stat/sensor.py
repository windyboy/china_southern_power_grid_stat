"""Sensors for the China Southern Power Grid Statistics integration."""

from __future__ import annotations

import asyncio
import copy
import datetime
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, STATE_UNAVAILABLE, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from . import CONF_UPDATED_AT
from .config import async_get_csg_clientsession, get_configured_update_interval
from .const import (
    ATTR_KEY_CURRENT_LADDER_START_DATE,
    ATTR_KEY_LAST_MONTH_BY_DAY,
    ATTR_KEY_LAST_YEAR_BY_MONTH,
    ATTR_KEY_LATEST_DAY_DATE,
    ATTR_KEY_THIS_MONTH_BY_DAY,
    ATTR_KEY_THIS_YEAR_BY_MONTH,
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    DATA_KEY_LAST_UPDATE_DAY,
    DOMAIN,
    SETTING_LAST_MONTH_UPDATE_DAY_THRESHOLD,
    SETTING_LAST_YEAR_UPDATE_DAY_THRESHOLD,
    SETTING_UPDATE_TIMEOUT,
    STATE_UPDATE_UNCHANGED,
    SZ_AREA_CODE,
    SZ_TIER_BOUNDARIES_SUMMER,
    SZ_TIER_BOUNDARIES_WINTER,
    SZ_TIER_TARIFFS,
    SUFFIX_ARR,
    SUFFIX_BAL,
    SUFFIX_CURRENT_LADDER,
    SUFFIX_CURRENT_LADDER_REMAINING_KWH,
    SUFFIX_CURRENT_LADDER_TARIFF,
    SUFFIX_LAST_MONTH_COST,
    SUFFIX_LAST_MONTH_KWH,
    SUFFIX_LAST_YEAR_COST,
    SUFFIX_LAST_YEAR_KWH,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_THIS_MONTH_COST,
    SUFFIX_THIS_MONTH_KWH,
    SUFFIX_THIS_YEAR_COST,
    SUFFIX_THIS_YEAR_KWH,
    SUFFIX_YESTERDAY_KWH,
)
from .csg_client import (
    JSON_KEY_METERING_POINT_NUMBER,
    WF_ATTR_CHARGE,
    WF_ATTR_DATE,
    WF_ATTR_KWH,
    WF_ATTR_LADDER,
    WF_ATTR_LADDER_REMAINING_KWH,
    WF_ATTR_LADDER_START_DATE,
    WF_ATTR_LADDER_TARIFF,
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
    CSGTransportError,
    NotLoggedIn,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Setup sensors from a config entry created in the integrations UI."""
    if not config_entry.data[CONF_ELE_ACCOUNTS]:
        _LOGGER.info("No ele accounts in config, exit entry setup")
        return
    coordinator = CSGCoordinator(hass, config_entry.entry_id)
    device_reg = dr.async_get(hass)

    all_sensors = []
    for ele_account_number in config_entry.data[CONF_ELE_ACCOUNTS]:
        legacy_identifier = (DOMAIN, ele_account_number)
        scoped_identifier = (
            DOMAIN,
            f"{config_entry.entry_id}:{ele_account_number}",
        )
        legacy_device = device_reg.async_get_device(identifiers={legacy_identifier})
        if legacy_device is not None and scoped_identifier not in legacy_device.identifiers:
            device_reg.async_update_device(
                legacy_device.id, new_identifiers={scoped_identifier}
            )
        sensors = [
            sensor_class(
                coordinator,
                ele_account_number,
                suffix,
                extra_state_attributes_key=extra_state_attributes_key,
            )
            for sensor_class, suffix, extra_state_attributes_key in _SENSOR_DEFINITIONS
        ]

        all_sensors.extend(sensors)

    async_add_entities(all_sensors)
    _LOGGER.debug(
        "Created %s sensors for config %s", len(all_sensors), config_entry.title
    )
    # Schedule the first update to run in the background
    config_entry.async_create_task(
        hass,
        coordinator.async_config_entry_first_refresh(),
        f"{config_entry.title}_first_update",
    )


class CSGBaseSensor(
    CoordinatorEntity,
    SensorEntity,
):
    """Base CSG sensor"""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        account_number: str,
        entity_suffix: str,
        extra_state_attributes_key: str | None = None,
    ) -> None:
        SensorEntity.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._coordinator = coordinator
        self._account_number = account_number
        self._config_entry_id = getattr(coordinator, "_config_entry_id", None)

        self._entity_suffix = entity_suffix
        self._attr_extra_state_attributes = {}
        self._extra_state_attributes_key = extra_state_attributes_key

    @property
    def unique_id(self) -> str | None:
        if self._config_entry_id:
            return f"{DOMAIN}.{self._config_entry_id}.{self._account_number}.{self._entity_suffix}"
        return f"{DOMAIN}.{self._account_number}.{self._entity_suffix}"

    @property
    def name(self) -> str | None:
        return f"{self._account_number}-{self._entity_suffix}"

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"{self._config_entry_id}:{self._account_number}",
                )
            },
            name=f"CSGAccount-{self._account_number}",
            manufacturer="CSG",
            model="CSG Virtual Electricity Meter",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # _LOGGER.debug(
        #     "%s coordinator update triggered",
        #     self.unique_id,
        # )

        if not self._coordinator.data:
            _LOGGER.error(
                "%s coordinator has no data",
                self.unique_id,
            )
            self._attr_available = False
            self.async_write_ha_state()
            return

        account_data = self._coordinator.data.get(self._account_number)
        if account_data is None:
            _LOGGER.warning("%s not found in coordinator data", self.unique_id)
            self._attr_available = False
            self.async_write_ha_state()
            return

        new_native_value = account_data.get(self._entity_suffix)
        if new_native_value is None:
            _LOGGER.warning("%s data not found in coordinator data", self.unique_id)
            self._attr_available = False
            self.async_write_ha_state()
            return

        if new_native_value == STATE_UNAVAILABLE:
            _LOGGER.debug("%s data is unavailable", self.unique_id)
            self.async_write_ha_state()
            self._attr_available = False
            return

        # from this point the value is available
        self._attr_available = True

        if new_native_value == STATE_UPDATE_UNCHANGED:
            # no update for this sensor, skip
            _LOGGER.debug("%s doesn't need to be updated, skip", self.unique_id)
            return

        # from this point, `new_native_value` is a true value
        self._attr_native_value = new_native_value

        if self._extra_state_attributes_key:
            new_attributes = account_data.get(self._extra_state_attributes_key)
            if new_attributes is None:
                new_attributes = {}
                _LOGGER.warning(
                    "%s attribute %s not found in coordinator data",
                    self.unique_id,
                    self._extra_state_attributes_key,
                )
            self._attr_extra_state_attributes = new_attributes
        _LOGGER.debug("%s state update done!", self.unique_id)
        self.async_write_ha_state()


class CSGEnergySensor(CSGBaseSensor):
    """Representation of a CSG Energy Sensor."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        account_number: str,
        entity_suffix: str,
        extra_state_attributes_key: str | None = None,
    ) -> None:
        super().__init__(
            coordinator,
            account_number,
            entity_suffix,
            extra_state_attributes_key,
        )
        self._attr_state_class = None
        if entity_suffix in (SUFFIX_THIS_MONTH_KWH, SUFFIX_THIS_YEAR_KWH):
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif entity_suffix == SUFFIX_CURRENT_LADDER_REMAINING_KWH:
            self._attr_device_class = SensorDeviceClass.ENERGY_STORAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT


class CSGCostSensor(CSGBaseSensor):
    """Representation of a CSG Cost Sensor."""

    _attr_native_unit_of_measurement = "CNY"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_icon = "mdi:currency-cny"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        account_number: str,
        entity_suffix: str,
        extra_state_attributes_key: str | None = None,
    ) -> None:
        super().__init__(
            coordinator,
            account_number,
            entity_suffix,
            extra_state_attributes_key,
        )
        self._attr_state_class = None
        if entity_suffix in (SUFFIX_THIS_MONTH_COST, SUFFIX_THIS_YEAR_COST):
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif entity_suffix == SUFFIX_CURRENT_LADDER_TARIFF:
            self._attr_device_class = None
            self._attr_native_unit_of_measurement = "CNY/kWh"


class CSGLadderStageSensor(CSGBaseSensor):
    """Representation of a CSG Ladder Stage Sensor."""

    _attr_icon = "mdi:stairs"

_SENSOR_DEFINITIONS: tuple[tuple[type, str, str | None], ...] = (
    (CSGCostSensor, SUFFIX_BAL, None),
    (CSGCostSensor, SUFFIX_ARR, None),
    (CSGEnergySensor, SUFFIX_YESTERDAY_KWH, None),
    (CSGEnergySensor, SUFFIX_LATEST_DAY_KWH, ATTR_KEY_LATEST_DAY_DATE),
    (CSGCostSensor, SUFFIX_LATEST_DAY_COST, ATTR_KEY_LATEST_DAY_DATE),
    (CSGEnergySensor, SUFFIX_THIS_YEAR_KWH, ATTR_KEY_THIS_YEAR_BY_MONTH),
    (CSGCostSensor, SUFFIX_THIS_YEAR_COST, None),
    (CSGEnergySensor, SUFFIX_THIS_MONTH_KWH, ATTR_KEY_THIS_MONTH_BY_DAY),
    (CSGCostSensor, SUFFIX_THIS_MONTH_COST, ATTR_KEY_THIS_MONTH_BY_DAY),
    (CSGLadderStageSensor, SUFFIX_CURRENT_LADDER, ATTR_KEY_CURRENT_LADDER_START_DATE),
    (CSGEnergySensor, SUFFIX_CURRENT_LADDER_REMAINING_KWH, None),
    (CSGCostSensor, SUFFIX_CURRENT_LADDER_TARIFF, None),
    (CSGEnergySensor, SUFFIX_LAST_YEAR_KWH, ATTR_KEY_LAST_YEAR_BY_MONTH),
    (CSGCostSensor, SUFFIX_LAST_YEAR_COST, None),
    (CSGEnergySensor, SUFFIX_LAST_MONTH_KWH, ATTR_KEY_LAST_MONTH_BY_DAY),
    (CSGCostSensor, SUFFIX_LAST_MONTH_COST, ATTR_KEY_LAST_MONTH_BY_DAY),
)


class CSGCoordinator(DataUpdateCoordinator):
    """CSG custom coordinator."""

    def __init__(self, hass: HomeAssistant, config_entry_id: str) -> None:
        """Initialize coordinator."""
        self._config_entry_id = config_entry_id
        config_entry = hass.config_entries.async_get_entry(self._config_entry_id)
        if config_entry is None:
            raise ValueError(f"Config entry {self._config_entry_id} not found")
        self._config = config_entry.data
        self._config_entry = config_entry
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name=f"CSG Account {self._config[CONF_USERNAME]}",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(
                seconds=get_configured_update_interval(config_entry)
            ),
        )
        self._client: CSGClient | None = None
        self._if_update_last_month = True
        self._if_update_last_year = True
        self._this_day = None
        self._this_year = None
        self._this_month_ym = None
        self._last_year = None
        self._last_month_ym = None
        self._gathered_data = {}

    async def _async_refresh_client(self):
        """Refresh the client, update the user data.
        It cannot re-login if the session is invalidated.
        """
        _LOGGER.debug("Refreshing client")
        self._client = CSGClient.load(
            {
                CONF_AUTH_TOKEN: self._config[CONF_AUTH_TOKEN],
            },
            async_get_csg_clientsession(self.hass, self._config_entry),
        )
        logged_in = await self._client.verify_login()
        if not logged_in:
            _LOGGER.warning("%s: Login expired", self._config[CONF_USERNAME])
            raise ConfigEntryAuthFailed("Login expired")

        _LOGGER.debug("%s: Session still valid", self._config[CONF_USERNAME])
        await self._client.initialize()

    async def _async_fetch(self, func: callable, *args, **kwargs) -> (bool, tuple):
        """Wrapper to fetch data from API. Return (success, result) with timeout.
        Also handle all exceptions here to avoid task group being cancelled.
        """
        try:
            async with asyncio.timeout(SETTING_UPDATE_TIMEOUT):
                return True, await func(*args, **kwargs)

        except asyncio.TimeoutError as err:
            _LOGGER.error("Timeout fetching data in function: %s", func.__name__)
            return False, (func.__name__, err)
        except NotLoggedIn as err:
            _LOGGER.error(
                "Session invalidated unexpectedly in function: %s", func.__name__
            )
            return False, (func.__name__, err)
        except CSGTransportError as err:
            _LOGGER.error(
                "Transport error fetching data in function %s: %s",
                func.__name__,
                err,
            )
            return False, (func.__name__, err)
        except CSGAPIError as err:
            _LOGGER.error(
                "Error fetching data in coordinator: API error, function %s, %s",
                func.__name__,
                err,
            )
            return False, (func.__name__, err)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception in %s", func.__name__)
            return False, (func.__name__, err)

    async def _async_fetch_year_stats(
        self, account: CSGElectricityAccount, year: int
    ) -> tuple:
        """Fetch one year's cost, kWh, and monthly breakdown."""
        success, result = await self._async_fetch(
            self._client.get_year_month_stats, account, year
        )
        if success:
            return result
        # CSG server-side hiccup: keep last known values instead of going unavailable
        return STATE_UPDATE_UNCHANGED, STATE_UPDATE_UNCHANGED, STATE_UPDATE_UNCHANGED

    async def _async_fetch_month_details(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ):
        """Fetch monthly usage and cost detail in parallel."""
        task_usage = asyncio.create_task(
            self._async_fetch(
                self._client.get_month_daily_usage_detail, account, year_month
            )
        )
        task_cost = asyncio.create_task(
            self._async_fetch(
                self._client.get_month_daily_cost_detail, account, year_month
            )
        )
        (success_usage, result_usage), (success_cost, result_cost) = await asyncio.gather(
            task_usage, task_cost
        )
        return success_usage, result_usage, success_cost, result_cost

    async def _async_update_bal_arr(self, account: CSGElectricityAccount):
        """Update balance and arrears"""
        success, result = await self._async_fetch(
            self._client.get_balance_and_arrears, account
        )
        if success:
            balance, arrears = result
            _LOGGER.debug(
                "Updated balance and arrears for account %s: %s",
                account.account_number,
                result,
            )
        else:
            balance, arrears = STATE_UPDATE_UNCHANGED, STATE_UPDATE_UNCHANGED
            _LOGGER.error(
                "Error updating balance and arrears for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][SUFFIX_BAL] = balance
        self._gathered_data[account.account_number][SUFFIX_ARR] = arrears

    async def _async_update_yesterday_kwh(self, account: CSGElectricityAccount):
        """Update yesterday's kwh"""
        if account.area_code == SZ_AREA_CODE:
            # Shenzhen: legacy yesterday endpoint returns empty; use the last
            # day of the electricity calendar.
            success, result = await self._async_fetch(
                self._client.get_month_daily_usage_detail_sz,
                account,
                self._this_month_ym,
            )
            if success and result:
                _, by_day = result
                if by_day:
                    yesterday_kwh = by_day[-1][WF_ATTR_KWH]
                    _LOGGER.debug(
                        "Updated yesterday's kwh for account %s: %s",
                        account.account_number,
                        yesterday_kwh,
                    )
                else:
                    yesterday_kwh = STATE_UNAVAILABLE
                    _LOGGER.error(
                        "No daily data for yesterday's kwh for account %s",
                        account.account_number,
                    )
            else:
                yesterday_kwh = STATE_UNAVAILABLE
                _LOGGER.error(
                    "Error updating yesterday's kwh for account %s: %s",
                    account.account_number,
                    result,
                )
            self._gathered_data[account.account_number][
                SUFFIX_YESTERDAY_KWH
            ] = yesterday_kwh
            return
        success, result = await self._async_fetch(
            self._client.get_yesterday_kwh,
            account,
        )
        if success and result is not None:
            yesterday_kwh = result
            _LOGGER.debug(
                "Updated yesterday's kwh for account %s: %s",
                account.account_number,
                result,
            )
        else:
            yesterday_kwh = STATE_UPDATE_UNCHANGED
            _LOGGER.error(
                "Error updating yesterday's kwh for account %s: %s",
                account.account_number,
                result,
            )
        self._gathered_data[account.account_number][
            SUFFIX_YESTERDAY_KWH
        ] = yesterday_kwh

    async def _async_update_this_year_stats(self, account: CSGElectricityAccount):
        """Update this year's data."""
        this_year_cost, this_year_kwh, this_year_by_month = (
            await self._async_fetch_year_stats(account, self._this_year)
        )
        self._gathered_data[account.account_number][
            SUFFIX_THIS_YEAR_KWH
        ] = this_year_kwh
        self._gathered_data[account.account_number][
            SUFFIX_THIS_YEAR_COST
        ] = this_year_cost
        self._gathered_data[account.account_number][ATTR_KEY_THIS_YEAR_BY_MONTH] = {
            ATTR_KEY_THIS_YEAR_BY_MONTH: this_year_by_month
        }

    async def _async_update_last_year_stats(self, account: CSGElectricityAccount):
        """Update last year's data"""
        if not self._if_update_last_year:
            self._gathered_data[account.account_number][
                SUFFIX_LAST_YEAR_KWH
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                SUFFIX_LAST_YEAR_COST
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][ATTR_KEY_LAST_YEAR_BY_MONTH] = {
                ATTR_KEY_LAST_YEAR_BY_MONTH: STATE_UPDATE_UNCHANGED
            }
            _LOGGER.debug(
                "Last year's data for account %s: no need to update",
                account.account_number,
            )
            return
        last_year_cost, last_year_kwh, last_year_by_month = (
            await self._async_fetch_year_stats(account, self._last_year)
        )
        self._gathered_data[account.account_number][
            SUFFIX_LAST_YEAR_KWH
        ] = last_year_kwh
        self._gathered_data[account.account_number][
            SUFFIX_LAST_YEAR_COST
        ] = last_year_cost
        self._gathered_data[account.account_number][ATTR_KEY_LAST_YEAR_BY_MONTH] = {
            ATTR_KEY_LAST_YEAR_BY_MONTH: last_year_by_month
        }

    @staticmethod
    def merge_by_day_data(
        by_day_from_cost: list | str,
        kwh_from_cost: float | str,
        by_day_from_usage: list | str,
        kwh_from_usage: float | str,
    ) -> (list | str, float | str):
        """Merge by_day_from_usage and by_day_from_cost data"""
        # merge by_day
        # determine which is the latest
        if (
            by_day_from_cost == STATE_UNAVAILABLE
            and by_day_from_usage == STATE_UNAVAILABLE
        ):
            by_day = STATE_UNAVAILABLE
        elif by_day_from_cost == STATE_UNAVAILABLE:
            by_day = by_day_from_usage
        elif by_day_from_usage == STATE_UNAVAILABLE:
            by_day = by_day_from_cost
        else:
            # both are available
            if len(by_day_from_cost) >= len(by_day_from_usage):
                # the result from daily cost is newer
                by_day = by_day_from_cost
            else:
                # the result from daily usage is newer
                # but since the result from daily cost contains cost data, need to merge them
                by_day = by_day_from_usage
                for idx, item in enumerate(by_day_from_cost):
                    by_day[idx][WF_ATTR_CHARGE] = item[WF_ATTR_CHARGE]

        # determine which one to use as kwh
        if kwh_from_cost == STATE_UNAVAILABLE and kwh_from_usage == STATE_UNAVAILABLE:
            kwh = STATE_UNAVAILABLE
        elif kwh_from_cost == STATE_UNAVAILABLE:
            kwh = kwh_from_usage
        elif kwh_from_usage == STATE_UNAVAILABLE:
            kwh = kwh_from_cost
        else:
            # determine which kwh is the latest
            # get the larger one
            kwh = max(kwh_from_cost, kwh_from_usage)
        return by_day, kwh

    @staticmethod
    def _calc_ladder_from_kwh(kwh: float, year: int, month: int):
        """Estimate Shenzhen ladder stage from month usage.

        CSG no longer returns live ladder data for Shenzhen accounts (the
        legacy queryDayElectricChargeByMPoint endpoint returns empty since the
        2026-09 migration). The app does not display tiers either. We estimate
        from the month usage using the published monthly tier boundaries:
        summer (May-Oct) 350/700 kWh, non-summer 200/400 kWh. Tariffs are the
        exact values from the monthly bill PDF.
        """
        if 5 <= month <= 10:
            stage1, stage2 = SZ_TIER_BOUNDARIES_SUMMER
        else:
            stage1, stage2 = SZ_TIER_BOUNDARIES_WINTER
        if kwh <= stage1:
            stage, remaining, tariff = 1, stage1 - kwh, SZ_TIER_TARIFFS[0]
        elif kwh <= stage2:
            stage, remaining, tariff = 2, stage2 - kwh, SZ_TIER_TARIFFS[1]
        else:
            stage, remaining, tariff = 3, 0.0, SZ_TIER_TARIFFS[2]
        return stage, remaining, tariff

    async def _async_update_this_month_stats_and_ladder(
        self, account: CSGElectricityAccount
    ):
        """Update this month's usage, cost and ladder"""
        if account.area_code == SZ_AREA_CODE:
            # Shenzhen: legacy daily endpoints return "没有返回数据:null" since
            # the 2026-09 CSG migration. Use the electricity calendar endpoint
            # (what the app uses) plus the monthly bill list.
            success_usage, result_usage = await self._async_fetch(
                self._client.get_month_daily_usage_detail_sz,
                account,
                self._this_month_ym,
            )
            if success_usage and result_usage:
                this_month_kwh, this_month_by_day = result_usage
            else:
                this_month_kwh, this_month_by_day = (
                    STATE_UNAVAILABLE,
                    STATE_UNAVAILABLE,
                )

            success_cost, result_cost = await self._async_fetch(
                self._client.get_month_bill_list, account, self._this_month_ym
            )
            if success_cost and isinstance(result_cost, dict):
                # current-month bill only exists after month close
                this_month_cost = float(result_cost.get("totalElectricity") or 0)
            else:
                this_month_cost = STATE_UNAVAILABLE

            if isinstance(this_month_kwh, (int, float)):
                (
                    ladder_stage,
                    ladder_remaining_kwh,
                    ladder_tariff,
                ) = self._calc_ladder_from_kwh(
                    this_month_kwh, self._this_month_ym[0], self._this_month_ym[1]
                )
                ladder_start_date = None
            else:
                ladder_stage = STATE_UNAVAILABLE
                ladder_remaining_kwh = STATE_UNAVAILABLE
                ladder_tariff = STATE_UNAVAILABLE
                ladder_start_date = STATE_UNAVAILABLE

            self._gathered_data[account.account_number][
                SUFFIX_THIS_MONTH_KWH
            ] = this_month_kwh
            self._gathered_data[account.account_number][
                SUFFIX_THIS_MONTH_COST
            ] = this_month_cost
            self._gathered_data[account.account_number][ATTR_KEY_THIS_MONTH_BY_DAY] = {
                ATTR_KEY_THIS_MONTH_BY_DAY: this_month_by_day
            }
            self._gathered_data[account.account_number][
                SUFFIX_CURRENT_LADDER
            ] = ladder_stage
            self._gathered_data[account.account_number][
                SUFFIX_CURRENT_LADDER_REMAINING_KWH
            ] = ladder_remaining_kwh
            self._gathered_data[account.account_number][
                SUFFIX_CURRENT_LADDER_TARIFF
            ] = ladder_tariff
            self._gathered_data[account.account_number][
                ATTR_KEY_CURRENT_LADDER_START_DATE
            ] = {ATTR_KEY_CURRENT_LADDER_START_DATE: ladder_start_date}
            if this_month_by_day == STATE_UNAVAILABLE:
                # latest_day needs by-day data; fall back to last month
                self._if_update_last_month = True
            return

        (
            success_usage,
            result_usage,
            success_cost,
            result_cost,
        ) = await self._async_fetch_month_details(account, self._this_month_ym)

        if success_usage and result_usage:
            this_month_kwh_from_usage, this_month_by_day_from_usage = result_usage
        else:
            this_month_kwh_from_usage = STATE_UNAVAILABLE
            this_month_by_day_from_usage = STATE_UNAVAILABLE

        if success_cost and result_cost:
            (
                this_month_cost,
                this_month_kwh_from_cost,
                ladder,
                this_month_by_day_from_cost,
            ) = result_cost
            # special processing
            if this_month_cost is None:
                this_month_cost = STATE_UNAVAILABLE
            if this_month_kwh_from_cost is None:
                this_month_kwh_from_cost = STATE_UNAVAILABLE
            if ladder and isinstance(ladder, dict):
                ladder_stage = ladder.get(WF_ATTR_LADDER) or STATE_UNAVAILABLE
                ladder_remaining_kwh = (
                    ladder.get(WF_ATTR_LADDER_REMAINING_KWH) or STATE_UNAVAILABLE
                )
                ladder_tariff = ladder.get(WF_ATTR_LADDER_TARIFF) or STATE_UNAVAILABLE
                ladder_start_date = (
                    ladder.get(WF_ATTR_LADDER_START_DATE) or STATE_UNAVAILABLE
                )
            else:
                ladder_stage = STATE_UNAVAILABLE
                ladder_remaining_kwh = STATE_UNAVAILABLE
                ladder_tariff = STATE_UNAVAILABLE
                ladder_start_date = STATE_UNAVAILABLE
        else:
            (
                this_month_cost,
                this_month_kwh_from_cost,
                this_month_by_day_from_cost,
                ladder_stage,
                ladder_remaining_kwh,
                ladder_tariff,
                ladder_start_date,
            ) = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )
        if (not success_usage or not result_usage) and (
            not success_cost or not result_cost
        ):
            # Both daily usage and daily cost queries failed (CSG server-side
            # hiccup, e.g. "没有返回数据:null"). Keep last known values instead
            # of marking the entities unavailable; they will refresh on the
            # next successful poll.
            _LOGGER.warning(
                "Both daily usage and cost queries failed for account %s; keeping last known values",
                account.account_number,
            )
            self._gathered_data[account.account_number][
                SUFFIX_THIS_MONTH_KWH
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                SUFFIX_THIS_MONTH_COST
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][ATTR_KEY_THIS_MONTH_BY_DAY] = {
                ATTR_KEY_THIS_MONTH_BY_DAY: STATE_UPDATE_UNCHANGED
            }
            self._gathered_data[account.account_number][
                SUFFIX_CURRENT_LADDER
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                SUFFIX_CURRENT_LADDER_REMAINING_KWH
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                SUFFIX_CURRENT_LADDER_TARIFF
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                ATTR_KEY_CURRENT_LADDER_START_DATE
            ] = {ATTR_KEY_CURRENT_LADDER_START_DATE: STATE_UPDATE_UNCHANGED}
            # latest_day needs by-day data; fall back to last month
            self._if_update_last_month = True
            return

        this_month_by_day, this_month_kwh = self.merge_by_day_data(
            by_day_from_usage=this_month_by_day_from_usage,
            kwh_from_usage=this_month_kwh_from_usage,
            by_day_from_cost=this_month_by_day_from_cost,
            kwh_from_cost=this_month_kwh_from_cost,
        )

        if this_month_by_day == STATE_UNAVAILABLE:
            # need last month's data to update `latest_day` entity
            self._if_update_last_month = True

        self._gathered_data[account.account_number][
            SUFFIX_THIS_MONTH_KWH
        ] = this_month_kwh
        self._gathered_data[account.account_number][
            SUFFIX_THIS_MONTH_COST
        ] = this_month_cost
        self._gathered_data[account.account_number][ATTR_KEY_THIS_MONTH_BY_DAY] = {
            ATTR_KEY_THIS_MONTH_BY_DAY: this_month_by_day
        }
        self._gathered_data[account.account_number][
            SUFFIX_CURRENT_LADDER
        ] = ladder_stage
        self._gathered_data[account.account_number][
            SUFFIX_CURRENT_LADDER_REMAINING_KWH
        ] = ladder_remaining_kwh
        self._gathered_data[account.account_number][
            SUFFIX_CURRENT_LADDER_TARIFF
        ] = ladder_tariff
        self._gathered_data[account.account_number][
            ATTR_KEY_CURRENT_LADDER_START_DATE
        ] = {ATTR_KEY_CURRENT_LADDER_START_DATE: ladder_start_date}

    async def _async_update_last_month_stats(self, account: CSGElectricityAccount):
        """Update last month's usage and cost"""
        if not self._if_update_last_month:
            _LOGGER.debug(
                "Last month's data for account %s: no need to update",
                account.account_number,
            )
            self._gathered_data[account.account_number][
                SUFFIX_LAST_MONTH_KWH
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                SUFFIX_LAST_MONTH_COST
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                ATTR_KEY_LAST_MONTH_BY_DAY
            ] = {ATTR_KEY_LAST_MONTH_BY_DAY: STATE_UPDATE_UNCHANGED}
            return

        if account.area_code == SZ_AREA_CODE:
            # Shenzhen: use the monthly bill list (exact totals) plus the
            # electricity calendar for per-day data.
            success_bill, result_bill = await self._async_fetch(
                self._client.get_month_bill_list, account, self._last_month_ym
            )
            success_cal, result_cal = await self._async_fetch(
                self._client.get_month_daily_usage_detail_sz,
                account,
                self._last_month_ym,
            )
            if success_bill and isinstance(result_bill, dict):
                last_month_kwh = float(result_bill.get("totalPower") or 0)
                last_month_cost = float(result_bill.get("totalElectricity") or 0)
            else:
                last_month_kwh = STATE_UNAVAILABLE
                last_month_cost = STATE_UNAVAILABLE
            if success_cal and result_cal:
                _, last_month_by_day = result_cal
            else:
                last_month_by_day = STATE_UNAVAILABLE
            self._gathered_data[account.account_number][
                SUFFIX_LAST_MONTH_KWH
            ] = last_month_kwh
            self._gathered_data[account.account_number][
                SUFFIX_LAST_MONTH_COST
            ] = last_month_cost
            self._gathered_data[account.account_number][ATTR_KEY_LAST_MONTH_BY_DAY] = {
                ATTR_KEY_LAST_MONTH_BY_DAY: last_month_by_day
            }
            return

        # continue to update last month's data
        (
            success_usage,
            result_usage,
            success_cost,
            result_cost,
        ) = await self._async_fetch_month_details(account, self._last_month_ym)

        if success_usage and result_usage:
            last_month_kwh_from_usage, last_month_by_day_from_usage = result_usage
        else:
            last_month_kwh_from_usage = STATE_UNAVAILABLE
            last_month_by_day_from_usage = STATE_UNAVAILABLE

        if success_cost and result_cost:
            (
                last_month_cost,
                last_month_kwh_from_cost,
                _,  # ladder is discarded
                last_month_by_day_from_cost,
            ) = result_cost

            # for last month, it's safe to calculate total kwh from cost
            if not last_month_cost and isinstance(last_month_by_day_from_cost, list):
                last_month_cost = sum(
                    d[WF_ATTR_CHARGE] for d in last_month_by_day_from_cost
                )
            if not last_month_kwh_from_cost and isinstance(
                last_month_by_day_from_cost, list
            ):
                last_month_kwh_from_cost = sum(
                    d[WF_ATTR_KWH] for d in last_month_by_day_from_cost
                )
        else:
            (
                last_month_cost,
                last_month_kwh_from_cost,
                last_month_by_day_from_cost,
            ) = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )
        if (not success_usage or not result_usage) and (
            not success_cost or not result_cost
        ):
            # Both last-month queries failed (CSG server-side hiccup):
            # keep last known values instead of going unavailable.
            _LOGGER.warning(
                "Both last-month daily usage and cost queries failed for account %s; keeping last known values",
                account.account_number,
            )
            self._gathered_data[account.account_number][
                SUFFIX_LAST_MONTH_KWH
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                SUFFIX_LAST_MONTH_COST
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][ATTR_KEY_LAST_MONTH_BY_DAY] = {
                ATTR_KEY_LAST_MONTH_BY_DAY: STATE_UPDATE_UNCHANGED
            }
            return

        last_month_by_day, last_month_kwh = self.merge_by_day_data(
            by_day_from_usage=last_month_by_day_from_usage,
            kwh_from_usage=last_month_kwh_from_usage,
            by_day_from_cost=last_month_by_day_from_cost,
            kwh_from_cost=last_month_kwh_from_cost,
        )

        self._gathered_data[account.account_number][
            SUFFIX_LAST_MONTH_KWH
        ] = last_month_kwh
        self._gathered_data[account.account_number][
            SUFFIX_LAST_MONTH_COST
        ] = last_month_cost
        self._gathered_data[account.account_number][ATTR_KEY_LAST_MONTH_BY_DAY] = {
            ATTR_KEY_LAST_MONTH_BY_DAY: last_month_by_day
        }

    def _update_latest_day(self, account: CSGElectricityAccount):
        this_month_by_day = self._gathered_data[account.account_number][
            ATTR_KEY_THIS_MONTH_BY_DAY
        ][ATTR_KEY_THIS_MONTH_BY_DAY]
        last_month_by_day = self._gathered_data[account.account_number][
            ATTR_KEY_LAST_MONTH_BY_DAY
        ][ATTR_KEY_LAST_MONTH_BY_DAY]

        if (
            this_month_by_day == STATE_UNAVAILABLE
            and last_month_by_day == STATE_UNAVAILABLE
        ):
            latest_day_kwh = STATE_UNAVAILABLE
            latest_day_cost = STATE_UNAVAILABLE
            latest_day_date = STATE_UNAVAILABLE
        else:
            if (
                this_month_by_day
                not in (STATE_UNAVAILABLE, STATE_UPDATE_UNCHANGED)
                and len(this_month_by_day) >= 1
            ):
                # we have this month's data, use the latest day
                latest_day_kwh = this_month_by_day[-1][WF_ATTR_KWH]
                latest_day_cost = (
                    this_month_by_day[-1].get(WF_ATTR_CHARGE) or STATE_UNAVAILABLE
                )
                latest_day_date = this_month_by_day[-1][WF_ATTR_DATE]
            else:
                # this month isn't available yet (typically during the first 3 days)
                # let's try last month
                if (
                    last_month_by_day
                    not in [
                        STATE_UNAVAILABLE,
                        STATE_UPDATE_UNCHANGED,
                    ]
                    and len(last_month_by_day) >= 1
                ):
                    latest_day_kwh = last_month_by_day[-1][WF_ATTR_KWH]
                    latest_day_cost = STATE_UNAVAILABLE
                    latest_day_date = last_month_by_day[-1][WF_ATTR_DATE]
                else:
                    _LOGGER.error(
                        "Ele account %s, no latest day data available",
                        account.account_number,
                    )
                    latest_day_kwh = STATE_UNAVAILABLE
                    latest_day_cost = STATE_UNAVAILABLE
                    latest_day_date = STATE_UNAVAILABLE
        self._gathered_data[account.account_number][
            SUFFIX_LATEST_DAY_KWH
        ] = latest_day_kwh
        self._gathered_data[account.account_number][
            SUFFIX_LATEST_DAY_COST
        ] = latest_day_cost
        self._gathered_data[account.account_number][ATTR_KEY_LATEST_DAY_DATE] = {
            ATTR_KEY_LATEST_DAY_DATE: latest_day_date
        }

    def _update_states(self):
        current_dt = datetime.datetime.now()
        this_year, this_month, this_day = (
            current_dt.year,
            current_dt.month,
            current_dt.day,
        )
        last_year, last_month = this_year - 1, this_month - 1
        if last_month == 0:
            last_month_ym = (last_year, 12)
        else:
            last_month_ym = (this_year, last_month)
        self._this_day = this_day
        self._this_year = this_year
        self._this_month_ym = (this_year, this_month)
        self._last_year = last_year
        self._last_month_ym = last_month_ym

        # for last month and last year data, they won't change over a long period of time
        # so we could use cache
        #
        # update policy for last month:
        # for the first <LAST_MONTH_UPDATE_DAY_THRESHOLD> days of a month,
        # update every `update_interval`.
        # for the rest of the time, do not update.

        # update policy for last year:
        # for the first <LAST_YEAR_UPDATE_DAY_THRESHOLD> days of Jan, update daily at first update
        # for the rest of the time, do not update
        #
        # when integration is reloaded, all updates will be triggered
        # so user could just reload the integration to refresh the data if needed

        if (
            self.hass.data[DOMAIN][self._config_entry_id].get(DATA_KEY_LAST_UPDATE_DAY)
            is None
        ):
            # first update
            update_last_month = True
            update_last_year = True
            _LOGGER.debug(
                "First update for account %s, getting all past data",
                self._config[CONF_USERNAME],
            )
        else:
            update_last_month = False
            update_last_year = False

            if this_day <= SETTING_LAST_MONTH_UPDATE_DAY_THRESHOLD:
                update_last_month = True
            today_first_update_triggered = (
                self.hass.data[DOMAIN][self._config_entry_id][DATA_KEY_LAST_UPDATE_DAY]
                == this_day
            )
            if this_month == 1 and this_day <= SETTING_LAST_YEAR_UPDATE_DAY_THRESHOLD:
                if not today_first_update_triggered:
                    update_last_year = True
        self._if_update_last_month = update_last_month
        self._if_update_last_year = update_last_year

    async def _async_update_account_data(self, account: CSGElectricityAccount):
        start_time = time.time()
        results = await asyncio.gather(
            self._async_update_bal_arr(account),
            self._async_update_yesterday_kwh(account),
            self._async_update_this_year_stats(account),
            self._async_update_last_year_stats(account),
            self._async_update_this_month_stats_and_ladder(account),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                _LOGGER.error(
                    "Ele account %s update task failed: %s",
                    account.account_number,
                    result,
                )
        if isinstance(results[-1], BaseException):
            self._if_update_last_month = True
        last_month_result = await asyncio.gather(
            self._async_update_last_month_stats(account), return_exceptions=True
        )
        if isinstance(last_month_result[0], BaseException):
            _LOGGER.error(
                "Ele account %s last-month update failed: %s",
                account.account_number,
                last_month_result[0],
            )
        try:
            self._update_latest_day(account)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.error(
                "Ele account %s, update latest day data failed: %s",
                account.account_number,
                exc,
            )

        _LOGGER.debug(
            "Ele account %s, update took %s seconds",
            account.account_number,
            time.time() - start_time,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        self.update_interval = timedelta(
            seconds=get_configured_update_interval(self._config_entry)
        )
        self._update_states()
        # _LOGGER.debug("Coordinator update interval: %d", self.update_interval.seconds)
        _LOGGER.debug("Coordinator update started")
        start_time = time.time()

        config_entry_need_update = False
        await self._async_refresh_client()
        new_config = copy.deepcopy(dict(self._config))
        for account_number, account_data in self._config[CONF_ELE_ACCOUNTS].items():
            self._gathered_data[account_number] = {}
            account = CSGElectricityAccount.load(account_data)
            # handling the addition of metering point number
            if not account.metering_point_number:
                ok, metering_point_data = await self._async_fetch(
                    self._client.api_get_metering_point,
                    account.area_code,
                    account.ele_customer_id,
                )
                if ok and isinstance(metering_point_data, list):
                    for mp in metering_point_data:
                        if (
                            isinstance(mp, dict)
                            and mp.get("eleCustNumber") == account.account_number
                            and mp.get(JSON_KEY_METERING_POINT_NUMBER)
                        ):
                            config_entry_need_update = True
                            account.metering_point_number = mp[
                                JSON_KEY_METERING_POINT_NUMBER
                            ]
                            new_config[CONF_ELE_ACCOUNTS][
                                account_number
                            ] = account.dump()
                            break

            await self._async_update_account_data(account)
        if config_entry_need_update:
            new_config[CONF_UPDATED_AT] = str(int(time.time() * 1000))
            config_entry = self.hass.config_entries.async_get_entry(self._config_entry_id)
            if config_entry is not None:
                self.hass.config_entries.async_update_entry(
                    config_entry,
                    data=new_config,
                )
                _LOGGER.debug("Updated accounts with metering point number")
            else:
                _LOGGER.warning(
                    "Config entry %s not found, skipping data update",
                    self._config_entry_id,
                )
        _LOGGER.debug("Coordinator update took %s seconds", time.time() - start_time)
        self.hass.data[DOMAIN][self._config_entry_id][
            DATA_KEY_LAST_UPDATE_DAY
        ] = self._this_day
        return self._gathered_data
