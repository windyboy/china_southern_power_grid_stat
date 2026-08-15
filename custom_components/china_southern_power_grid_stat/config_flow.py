"""
Config flow for China Southern Power Grid Statistics integration.
Steps:
1. User input account credentials (username and password), the validity of credential verified
2. Get all electricity accounts linked to the user account, let user select one of them
3. Get the rest of needed parameters and save the config entries
"""
from __future__ import annotations

import copy
import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers import selector, translation

from .config import (
    async_get_csg_clientsession,
    async_get_csg_clientsession_for_family,
    get_configured_ip_family,
    get_configured_update_interval,
)
from .const import (
    ABORT_ALL_ADDED,
    ABORT_NO_ACCOUNT,
    CONF_ACCOUNT_NUMBER,
    CONF_ACTION,
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_GENERAL_ERROR,
    CONF_IP_FAMILY,
    CONF_LOGIN_TYPE,
    CONF_REFRESH_QR_CODE,
    CONF_SETTINGS,
    CONF_SMS_CODE,
    CONF_UPDATE_INTERVAL,
    CONF_UPDATED_AT,
    DEFAULT_IP_FAMILY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_AUTH,
    ERROR_QR_EXPIRED,
    ERROR_QR_NOT_SCANNED,
    ERROR_UNKNOWN,
    IP_FAMILY_OPTIONS,
    LOGIN_TYPE_TO_QR_APP_NAME,
    STEP_ADD_ACCOUNT,
    STEP_ALI_QR_LOGIN,
    STEP_CSG_QR_LOGIN,
    STEP_INIT,
    STEP_NETWORK,
    STEP_QR_LOGIN,
    STEP_SETTINGS,
    STEP_SMS_LOGIN,
    STEP_SMS_PWD_LOGIN,
    STEP_USER,
    STEP_VALIDATE_SMS_CODE,
    STEP_WX_QR_LOGIN,
)
from .csg_client import (
    LOGIN_TYPE_TO_QR_CODE_TYPE,
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
    CSGTransportError,
    InvalidCredentials,
    LoginType,
    QrCodeExpired,
)

_LOGGER = logging.getLogger(__name__)


class CSGConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for China Southern Power Grid Statistics."""

    VERSION = 1
    _reauth_entry: config_entries.ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return CSGOptionsFlowHandler(config_entry)

    def _default_ip_family(self) -> str:
        """Default value for the network step (reauth pre-fills the entry's mode)."""
        if self._reauth_entry is not None:
            return get_configured_ip_family(self._reauth_entry)
        return DEFAULT_IP_FAMILY

    def _get_ip_family(self) -> str:
        """Return the IP family selected in this flow."""
        return self.context.get("user_data", {}).get(CONF_IP_FAMILY, DEFAULT_IP_FAMILY)

    def _new_client(self) -> CSGClient:
        """Create a CSG client using the IP family selected in this flow."""
        return CSGClient(
            async_get_csg_clientsession_for_family(self.hass, self._get_ip_family())
        )

    async def _get_step_invalid_message(
        self, step: str, field: str, fallback: str
    ) -> str:
        """Return the translated 'invalid' message for a form field."""
        trans = await translation.async_get_translations(
            self.hass, self.hass.config.language, "config", {DOMAIN}
        )
        return trans.get(
            f"component.{DOMAIN}.config.step.{step}.data.{field}_invalid", fallback
        )

    def _show_login_method_menu(self) -> FlowResult:
        """Show the menu for choosing a login method."""
        return self.async_show_menu(
            step_id=STEP_USER,
            menu_options=[
                STEP_SMS_LOGIN,
                STEP_SMS_PWD_LOGIN,
                STEP_CSG_QR_LOGIN,
                STEP_WX_QR_LOGIN,
                STEP_ALI_QR_LOGIN,
            ],
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """
        Handle the initial step.
        Ask for the network IP family, then let the user choose a login method.
        """
        self.context["user_data"] = {}
        return await self.async_step_network()

    async def async_step_network(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the network IP family used to reach the CSG servers."""
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_IP_FAMILY, default=self._default_ip_family()
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(IP_FAMILY_OPTIONS),
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key=CONF_IP_FAMILY,
                        )
                    ),
                }
            )
            return self.async_show_form(step_id=STEP_NETWORK, data_schema=schema)
        self.context["user_data"][CONF_IP_FAMILY] = user_input[CONF_IP_FAMILY]
        return self._show_login_method_menu()

    async def async_step_sms_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle SMS login step."""
        if user_input is None:
            # initial step, need phone number to send SMS code
            msg_username = await self._get_step_invalid_message(
                STEP_SMS_LOGIN, "username", "请输入11位手机号"
            )
            return self.async_show_form(
                step_id=STEP_SMS_LOGIN,
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_USERNAME): vol.All(
                            str, vol.Length(min=11, max=11), msg=msg_username
                        )
                    }
                ),
            )
        self.context["user_data"][CONF_USERNAME] = user_input[CONF_USERNAME]
        self.context["user_data"][CONF_PASSWORD] = ""
        self.context["user_data"][CONF_LOGIN_TYPE] = LoginType.LOGIN_TYPE_SMS
        return await self.async_step_validate_sms_code()

    async def async_step_sms_pwd_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle SMS and password login step."""
        if user_input is None:
            msg_username = await self._get_step_invalid_message(
                STEP_SMS_PWD_LOGIN, "username", "请输入11位手机号"
            )
            msg_password = await self._get_step_invalid_message(
                STEP_SMS_PWD_LOGIN, "password", "请输入8-16位登陆密码"
            )
            return self.async_show_form(
                step_id=STEP_SMS_PWD_LOGIN,
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_USERNAME): vol.All(
                            str, vol.Length(min=11, max=11), msg=msg_username
                        ),
                        vol.Required(CONF_PASSWORD): vol.All(
                            str, vol.Length(min=8, max=16), msg=msg_password
                        ),
                    }
                ),
            )
        self.context["user_data"][CONF_USERNAME] = user_input[CONF_USERNAME]
        self.context["user_data"][CONF_PASSWORD] = user_input[CONF_PASSWORD]
        self.context["user_data"][CONF_LOGIN_TYPE] = LoginType.LOGIN_TYPE_PWD_AND_SMS
        return await self.async_step_validate_sms_code()

    async def async_step_validate_sms_code(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle SMS code validation step, for both SMS and SMS+password login."""
        msg_code = await self._get_step_invalid_message(
            STEP_VALIDATE_SMS_CODE, "sms_code", "请输入6位短信验证码"
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SMS_CODE): vol.All(
                    str, vol.Length(min=6, max=6), msg=msg_code
                ),
            }
        )
        client = self._new_client()
        username = self.context["user_data"][CONF_USERNAME]

        if user_input is None:
            await self.check_and_set_unique_id(username)
            errors = {}
            error_detail = ""
            try:
                await client.api_send_login_sms(username)
            except CSGTransportError:
                errors[CONF_GENERAL_ERROR] = ERROR_CANNOT_CONNECT
            except Exception as ge:
                _LOGGER.exception("Unexpected exception when sending sms code")
                errors[CONF_GENERAL_ERROR] = ERROR_UNKNOWN
                error_detail = str(ge)
                _LOGGER.debug("SMS send error detail: %s", error_detail)
            else:
                return self.async_show_form(
                    step_id=STEP_VALIDATE_SMS_CODE,
                    data_schema=schema,
                    description_placeholders={"phone_no": username},
                )
            return self.async_show_form(
                step_id=STEP_VALIDATE_SMS_CODE,
                data_schema=schema,
                errors=errors,
            )

        # sms code is present, validate with api
        password = self.context["user_data"][CONF_PASSWORD]
        login_type: LoginType = self.context["user_data"][CONF_LOGIN_TYPE]
        sms_code = user_input[CONF_SMS_CODE]

        errors = {}
        error_detail = ""
        try:
            if login_type == LoginType.LOGIN_TYPE_SMS:
                auth_token = await client.api_login_with_sms_code(
                    username, sms_code
                )
            elif login_type == LoginType.LOGIN_TYPE_PWD_AND_SMS:
                auth_token = await client.api_login_with_password_and_sms_code(
                    username,
                    password,
                    sms_code,
                )
            else:
                raise ValueError(
                    f"Invalid login type for step {STEP_VALIDATE_SMS_CODE}: {login_type}"
                )
        except CSGTransportError:
            errors[CONF_GENERAL_ERROR] = ERROR_CANNOT_CONNECT
        except InvalidCredentials as ice:
            errors[CONF_GENERAL_ERROR] = ERROR_INVALID_AUTH
            error_detail = str(ice)
            _LOGGER.debug("Login validation error detail: %s", error_detail)
        except Exception as ge:
            _LOGGER.exception("Unexpected exception during login validation")
            errors[CONF_GENERAL_ERROR] = ERROR_UNKNOWN
            error_detail = str(ge)
            _LOGGER.debug("Login validation error detail: %s", error_detail)
        else:
            return await self.create_or_update_config_entry(
                auth_token, login_type, password, username
            )
        return self.async_show_form(
            step_id=STEP_VALIDATE_SMS_CODE,
            data_schema=schema,
            errors=errors,
        )

    async def async_step_csg_qr_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """CSG APP QR Login"""
        self.context["user_data"][CONF_LOGIN_TYPE] = LoginType.LOGIN_TYPE_CSG_QR
        return await self.async_step_qr_login()

    async def async_step_wx_qr_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """WeChat QR Login"""
        self.context["user_data"][CONF_LOGIN_TYPE] = LoginType.LOGIN_TYPE_WX_QR
        return await self.async_step_qr_login()

    async def async_step_ali_qr_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """AliPay QR Login"""
        self.context["user_data"][CONF_LOGIN_TYPE] = LoginType.LOGIN_TYPE_ALI_QR
        return await self.async_step_qr_login()

    def _show_qr_form(
        self, login_type: LoginType, errors: dict[str, str] | None = None
    ) -> FlowResult:
        """Render the QR login form, reusing the stored QR image when present."""
        image_link = self.context["user_data"].get("image_link")
        description = (
            f"<p>使用{LOGIN_TYPE_TO_QR_APP_NAME[login_type]}扫码登录。"
            "登录完成后，点击下一步。</p>"
        )
        if image_link:
            description += (
                f'<img src="{image_link}" alt="QR code" style="width: 200px;"/>'
            )
        return self.async_show_form(
            step_id=STEP_QR_LOGIN,
            data_schema=vol.Schema(
                {vol.Required(CONF_REFRESH_QR_CODE, default=False): bool}
            ),
            errors=errors,
            # had to do this because strings.json conflicts with html tags
            description_placeholders={"description": description},
        )

    async def async_step_qr_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle QR code login step."""
        client = self._new_client()
        login_type = self.context["user_data"][CONF_LOGIN_TYPE]
        if user_input is None:
            # create QR code
            self.context["user_data"].pop("login_id", None)
            self.context["user_data"].pop("image_link", None)
            errors: dict[str, str] = {}
            try:
                login_id, image_link = await client.api_create_login_qr_code(
                    LOGIN_TYPE_TO_QR_CODE_TYPE[login_type]
                )
            except CSGTransportError:
                errors[CONF_GENERAL_ERROR] = ERROR_CANNOT_CONNECT
            except Exception as ge:
                _LOGGER.exception("Unexpected exception when creating QR code")
                _LOGGER.debug("QR create error detail: %s", ge)
                errors[CONF_GENERAL_ERROR] = ERROR_UNKNOWN
            else:
                self.context["user_data"]["login_id"] = login_id
                self.context["user_data"]["image_link"] = image_link
                return self._show_qr_form(login_type)
            return self._show_qr_form(login_type, errors=errors)
        if user_input[CONF_REFRESH_QR_CODE]:
            return await self.async_step_qr_login()
        if "login_id" not in self.context["user_data"]:
            return await self.async_step_qr_login()
        return await self.async_step_validate_qr_login()

    async def async_step_validate_qr_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Get QR scan status after user has scanned the code"""
        client = self._new_client()
        login_type = self.context["user_data"][CONF_LOGIN_TYPE]
        login_id = self.context["user_data"]["login_id"]
        try:
            ok, auth_token = await client.api_get_qr_login_status(login_id)
        except QrCodeExpired:
            self.context["user_data"].pop("login_id", None)
            self.context["user_data"].pop("image_link", None)
            return self._show_qr_form(
                login_type, errors={CONF_GENERAL_ERROR: ERROR_QR_EXPIRED}
            )
        except CSGTransportError:
            return self._show_qr_form(
                login_type, errors={CONF_GENERAL_ERROR: ERROR_CANNOT_CONNECT}
            )
        except Exception as ge:
            _LOGGER.exception("Unexpected exception when querying QR login status")
            _LOGGER.debug("QR status error detail: %s", ge)
            return self._show_qr_form(
                login_type, errors={CONF_GENERAL_ERROR: ERROR_UNKNOWN}
            )
        if ok:
            # for QR login, use mobile number as username
            client.set_authentication_params(auth_token)
            try:
                user_info = await client.api_get_user_info()
            except CSGTransportError:
                return self._show_qr_form(
                    login_type, errors={CONF_GENERAL_ERROR: ERROR_CANNOT_CONNECT}
                )
            except Exception as ge:
                _LOGGER.exception("Unexpected exception when fetching QR user info")
                _LOGGER.debug("QR user info error detail: %s", ge)
                return self._show_qr_form(
                    login_type, errors={CONF_GENERAL_ERROR: ERROR_UNKNOWN}
                )
            if not isinstance(user_info, dict) or not user_info.get("mobile"):
                _LOGGER.error("QR login response did not include a mobile number")
                return self._show_qr_form(
                    login_type, errors={CONF_GENERAL_ERROR: ERROR_UNKNOWN}
                )
            username = user_info["mobile"]
            await self.check_and_set_unique_id(username)
            return await self.create_or_update_config_entry(
                auth_token, login_type, "", username
            )

        # scan not detected, return to previous step
        return self._show_qr_form(
            login_type, errors={CONF_GENERAL_ERROR: ERROR_QR_NOT_SCANNED}
        )

    async def check_and_set_unique_id(self, username: str):
        """set unique id for the config entry, abort if already configured"""
        # TODO: username (mobile) may not be the best unique id
        unique_id = f"CSG-{username}"
        await self.async_set_unique_id(unique_id)
        if self._reauth_entry is not None:
            if self._reauth_entry.unique_id != unique_id:
                raise AbortFlow("unique_id_mismatch")
            return
        self._abort_if_unique_id_configured()

    async def create_or_update_config_entry(
        self, auth_token, login_type, password, username
    ) -> FlowResult:
        """Create or update config entry
        If the account is newly added, create a new entry
        If the account is already added (reauth), update the existing entry"""
        data = {
            CONF_USERNAME: username,
            CONF_LOGIN_TYPE: login_type,
            CONF_AUTH_TOKEN: auth_token,
            CONF_ELE_ACCOUNTS: {},
            CONF_SETTINGS: {
                CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
            },
            CONF_UPDATED_AT: str(int(time.time() * 1000)),
        }
        # handle normal creation and reauth
        if self._reauth_entry:
            # reauth
            # save the old config and only update the auth related data
            old_config = copy.deepcopy(dict(self._reauth_entry.data))
            data[CONF_ELE_ACCOUNTS] = old_config[CONF_ELE_ACCOUNTS]
            data[CONF_SETTINGS] = old_config[CONF_SETTINGS]
            new_options = dict(self._reauth_entry.options)
            new_options[CONF_IP_FAMILY] = self._get_ip_family()
            self.hass.config_entries.async_update_entry(
                self._reauth_entry, data=data, options=new_options
            )
            if not getattr(self._reauth_entry, "update_listeners", ()):
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            self._reauth_entry = None
            return self.async_abort(reason="reauth_successful")
        # normal creation
        # check if account already exists

        return self.async_create_entry(
            title=f"CSG-{username}",
            data=data,
            options={CONF_IP_FAMILY: self._get_ip_family()},
        )

    async def async_step_reauth(self, user_input=None):
        """Perform reauth upon an API authentication error."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Dialog that informs the user that reauth is required."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
            )
        return await self.async_step_user()


class CSGOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for China Southern Power Grid Statistics."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__()
        self._config_entry = config_entry
        self.all_electricity_accounts: list[CSGElectricityAccount] = []

    @property
    def config_entry(self) -> config_entries.ConfigEntry:
        """Return the config entry on all supported Home Assistant versions."""
        return self._config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""

        schema = vol.Schema(
            {
                vol.Required(CONF_ACTION, default=STEP_ADD_ACCOUNT): vol.In(
                    {
                        STEP_ADD_ACCOUNT: "添加已绑定的缴费号",
                        STEP_SETTINGS: "参数设置",
                    }
                ),
            }
        )
        if user_input:
            if user_input[CONF_ACTION] == STEP_ADD_ACCOUNT:
                return await self.async_step_add_account()
            if user_input[CONF_ACTION] == STEP_SETTINGS:
                return await self.async_step_settings()
        return self.async_show_form(step_id=STEP_INIT, data_schema=schema)

    async def async_step_add_account(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select one of the electricity accounts from current account"""
        # account_no: f'{account_no} ({name} {addr})'

        all_csg_config_entries = self.hass.config_entries.async_entries(DOMAIN)
        # get a list of all account numbers from all config entries
        all_account_numbers = []
        for config_entry in all_csg_config_entries:
            all_account_numbers.extend(config_entry.data[CONF_ELE_ACCOUNTS].keys())
        if user_input:
            account_num_to_add = user_input[CONF_ACCOUNT_NUMBER]
            for account in self.all_electricity_accounts:
                if account.account_number == account_num_to_add:
                    # store the account config in main entry instead of creating new entries
                    new_data = copy.deepcopy(dict(self.config_entry.data))
                    new_data[CONF_ELE_ACCOUNTS][account_num_to_add] = account.dump()
                    # this must be set or update won't be detected
                    new_data[CONF_UPDATED_AT] = str(int(time.time() * 1000))
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data=new_data,
                    )
                    _LOGGER.info(
                        "Added ele account to %s: %s",
                        self.config_entry.data[CONF_USERNAME],
                        account_num_to_add,
                    )
                    return self.async_create_entry(
                        title="",
                        data=dict(self.config_entry.options),
                    )
        # end of handling add account

        # start of getting all unbound accounts
        client = CSGClient.load(
            {
                CONF_AUTH_TOKEN: self.config_entry.data[CONF_AUTH_TOKEN],
            },
            async_get_csg_clientsession(self.hass, self.config_entry),
        )
        try:
            logged_in = await client.verify_login()
            if not logged_in:
                self.config_entry.async_start_reauth(self.hass)
                return self.async_abort(reason="reauth_required")
            await client.initialize()
            accounts = await client.get_all_electricity_accounts()
        except CSGTransportError:
            return self.async_show_form(
                step_id=STEP_ADD_ACCOUNT,
                data_schema=vol.Schema({}),
                errors={CONF_GENERAL_ERROR: ERROR_CANNOT_CONNECT},
            )
        except CSGAPIError:
            return self.async_show_form(
                step_id=STEP_ADD_ACCOUNT,
                data_schema=vol.Schema({}),
                errors={CONF_GENERAL_ERROR: ERROR_UNKNOWN},
            )
        self.all_electricity_accounts = accounts
        if not accounts:
            _LOGGER.warning(
                "No linked ele accounts found in csg account %s",
                self.config_entry.data[CONF_USERNAME],
            )
            return self.async_abort(reason=ABORT_NO_ACCOUNT)
        selections = {}
        for account in accounts:
            if account.account_number not in all_account_numbers:
                # avoid adding one ele account twice
                selections[account.account_number] = (
                    f"{account.account_number} ({account.user_name} {account.address})"
                )
        if not selections:
            _LOGGER.info(
                "Account %s: no ele account to add (all already added), abort",
                self.config_entry.data[CONF_USERNAME],
            )
            return self.async_abort(reason=ABORT_ALL_ADDED)

        schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNT_NUMBER): vol.In(selections),
            }
        )
        return self.async_show_form(
            step_id=STEP_ADD_ACCOUNT,
            data_schema=schema,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Settings of parameters"""
        update_interval = get_configured_update_interval(self.config_entry)
        ip_family = get_configured_ip_family(self.config_entry)
        schema = vol.Schema(
            {
                vol.Required(CONF_UPDATE_INTERVAL, default=update_interval): vol.All(
                    int, vol.Range(min=60), msg="刷新间隔不能低于60秒"
                ),
                vol.Required(CONF_IP_FAMILY, default=ip_family): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(IP_FAMILY_OPTIONS),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_IP_FAMILY,
                    )
                ),
            }
        )
        if user_input is None:
            return self.async_show_form(step_id=STEP_SETTINGS, data_schema=schema)

        new_options = dict(self.config_entry.options)
        new_options[CONF_UPDATE_INTERVAL] = user_input[CONF_UPDATE_INTERVAL]
        new_options[CONF_IP_FAMILY] = user_input[CONF_IP_FAMILY]
        return self.async_create_entry(title="", data=new_options)
