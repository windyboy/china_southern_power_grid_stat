"""
Asynchronous implementation of CSG's Web API.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
import time
from base64 import b64decode, b64encode
from copy import copy
from hashlib import md5
from typing import Any

import aiohttp
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

from .const import (
    AREACODE_FALLBACK,
    ATTR_ACCOUNT_NUMBER,
    ATTR_ADDRESS,
    ATTR_AREA_CODE,
    ATTR_AUTH_TOKEN,
    ATTR_ELE_CUSTOMER_ID,
    ATTR_METERING_POINT_ID,
    ATTR_METERING_POINT_NUMBER,
    ATTR_USER_NAME,
    BASE_PATH_APP,
    BASE_PATH_WEB,
    CREDENTIAL_PUBKEY,
    HEADER_CUST_NUMBER,
    HEADER_X_AUTH_TOKEN,
    JSON_KEY_ACCT_ID,
    JSON_KEY_AREA_CODE,
    JSON_KEY_CRED_TYPE,
    JSON_KEY_CUST_NUMBER,
    JSON_KEY_DATA,
    JSON_KEY_ELE_CUST_ID,
    JSON_KEY_LOGON_CHAN,
    JSON_KEY_MESSAGE,
    JSON_KEY_METERING_POINT_ID,
    JSON_KEY_METERING_POINT_NUMBER,
    JSON_KEY_PARAM,
    JSON_KEY_SMS_CODE,
    JSON_KEY_STA,
    JSON_KEY_YEAR_MONTH,
    LOGIN_TYPE_PHONE_CODE,
    LOGIN_TYPE_PHONE_PWD_CODE,
    LOGIN_TYPE_TO_QR_CODE_TYPE,
    LOGON_CHANNEL_HANDHELD_HALL,
    LoginType,
    PARAM_IV,
    PARAM_KEY,
    QRCodeType,
    RESP_STA_LOGIN_WRONG_CREDENTIAL,
    RESP_STA_NO_LOGIN,
    RESP_STA_NO_METERING_POINT,
    RESP_STA_QR_NOT_SCANNED,
    RESP_STA_QR_TIMEOUT,
    RESP_STA_SUCCESS,
    SEND_MSG_TYPE_VERIFICATION_CODE,
    VERIFICATION_CODE_TYPE_LOGIN,
    WF_ATTR_CHARGE,
    WF_ATTR_DATE,
    WF_ATTR_KWH,
    WF_ATTR_LADDER,
    WF_ATTR_LADDER_REMAINING_KWH,
    WF_ATTR_LADDER_START_DATE,
    WF_ATTR_LADDER_TARIFF,
    WF_ATTR_MONTH,
)

_LOGGER = logging.getLogger(__name__)


class CSGAPIError(Exception):
    """Generic API errors"""

    def __init__(self, sta: str, msg: str | None = None) -> None:
        """sta: status code, msg: message"""
        Exception.__init__(self)
        self.sta = sta
        self.msg = msg

    def __str__(self):
        return f"<CSGAPIError sta={self.sta} message={self.msg}>"


class CSGHTTPError(CSGAPIError):
    """Unexpected HTTP status code (!=200)"""

    def __init__(self, code: int) -> None:
        CSGAPIError.__init__(self, sta=f"HTTP{code}")
        self.status_code = code

    def __str__(self) -> str:
        return f"<CSGHTTPError code={self.status_code}>"


class CSGTransportError(CSGAPIError):
    """Network transport failure before a valid CSG response was received."""

    def __init__(self, msg: str) -> None:
        super().__init__(sta="TRANSPORT", msg=msg)

    def __str__(self) -> str:
        return f"<CSGTransportError message={self.msg}>"


class InvalidCredentials(CSGAPIError):
    """Wrong username+password combination (RESP_STA_LOGIN_WRONG_CREDENTIAL)"""

    def __str__(self):
        return f"<CSGInvalidCredentials sta={self.sta} message={self.msg}>"


class NotLoggedIn(CSGAPIError):
    """Not logged in or login expired (RESP_STA_NO_LOGIN)"""

    def __str__(self):
        return f"<CSGNotLoggedIn sta={self.sta} message={self.msg}>"


class QrCodeExpired(Exception):
    """QR code has expired"""


def generate_qr_login_id():
    """
    Generate a unique id for qr code login
    word-by-word copied from js code
    """
    rand_str = f"{int(time.time() * 1000)}{random.random()}"
    return md5(rand_str.encode()).hexdigest()


def encrypt_credential(password: str) -> str:
    """Use RSA+pubkey to encrypt password"""
    rsa_key = RSA.import_key(b64decode(CREDENTIAL_PUBKEY))
    credential_cipher = PKCS1_v1_5.new(rsa_key)
    encrypted_pwd = credential_cipher.encrypt(password.encode("utf8"))
    return b64encode(encrypted_pwd).decode()


def encrypt_params(params: dict) -> str:
    """Encrypt request parameters with byte-aligned zero padding."""
    json_cipher = AES.new(PARAM_KEY, AES.MODE_CBC, PARAM_IV)
    json_bytes = json.dumps(
        params, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    padding_length = AES.block_size - len(json_bytes) % AES.block_size
    encrypted = json_cipher.encrypt(json_bytes + b"\x00" * padding_length)
    return b64encode(encrypted).decode()


def decrypt_params(encrypted: str) -> dict:
    """Decrypt request message using AES with KEY, IV"""
    json_cipher = AES.new(PARAM_KEY, AES.MODE_CBC, PARAM_IV)
    decrypted = json_cipher.decrypt(b64decode(encrypted))
    # remove padding
    params = json.loads(decrypted.decode().strip("\x00"))
    return params


class CSGElectricityAccount:
    """Represents one electricity account, identified by account number (缴费号)"""

    def __init__(
        self,
        account_number: str | None = None,
        area_code: str | None = None,
        ele_customer_id: str | None = None,
        metering_point_id: str | None = None,
        metering_point_number: str | None = None,
        address: str | None = None,
        user_name: str | None = None,
    ) -> None:
        # the parameters are independent for each electricity account

        # the 16-digit billing number, as a unique identifier, not used in api for now
        self.account_number = account_number

        self.area_code = area_code

        # this may change on every login, alternative name in js code is `binding_id`
        self.ele_customer_id = ele_customer_id

        # in fact one account may have multiple metering points,
        # however for individual users there should only be one
        self.metering_point_id = metering_point_id
        self.metering_point_number = metering_point_number

        # for frontend display only
        self.address = address
        self.user_name = user_name

    def dump(self) -> dict[str, str]:
        """serialize this object"""
        return {
            ATTR_ACCOUNT_NUMBER: self.account_number,
            ATTR_AREA_CODE: self.area_code,
            ATTR_ELE_CUSTOMER_ID: self.ele_customer_id,
            ATTR_METERING_POINT_ID: self.metering_point_id,
            ATTR_METERING_POINT_NUMBER: self.metering_point_number,
            ATTR_ADDRESS: self.address,
            ATTR_USER_NAME: self.user_name,
        }

    @staticmethod
    def load(data: dict) -> CSGElectricityAccount:
        """deserialize this object"""
        for k in (
            ATTR_ACCOUNT_NUMBER,
            ATTR_AREA_CODE,
            ATTR_ELE_CUSTOMER_ID,
            ATTR_METERING_POINT_ID,
            ATTR_ADDRESS,
            ATTR_USER_NAME,
        ):
            if k not in data:
                raise ValueError(f"Missing key {k}")
        # ATTR_METERING_POINT_NUMBER is added in later version, skip check here
        # TODO: add ATTR_METERING_POINT_NUMBER to the check in the future
        account = CSGElectricityAccount(
            account_number=data[ATTR_ACCOUNT_NUMBER],
            area_code=data[ATTR_AREA_CODE],
            ele_customer_id=data[ATTR_ELE_CUSTOMER_ID],
            metering_point_id=data[ATTR_METERING_POINT_ID],
            metering_point_number=data.get(ATTR_METERING_POINT_NUMBER),
            address=data[ATTR_ADDRESS],
            user_name=data[ATTR_USER_NAME],
        )
        return account


class CSGClient:
    """
    Implementation of APIs from CSG iOS app interface.
    Parameters and consts are from web app js, however, these interfaces are virtually the same

    Do not call any functions starts with _api unless you are certain about what you're doing

    How to use:
    First call one of the functions to login (see example code)
    Then call `CSGClient.initialize` *important
    To get all linked electricity accounts, call `get_all_electricity_accounts`
    Use the account objects to call the utility functions and wrapped api functions
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth_token: str | None = None,
    ) -> None:
        self._session = session
        self._timeout = aiohttp.ClientTimeout(
            total=40,
            connect=5,
            sock_connect=5,
            sock_read=30,
        )
        self._common_headers = {
            "Host": "95598.csg.cn",
            "Content-Type": "application/json;charset=utf-8",
            "Origin": "file://",
            HEADER_X_AUTH_TOKEN: "",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko)",
            HEADER_CUST_NUMBER: "",
            "Accept-Language": "zh-CN,cn;q=0.9",
        }

        self.auth_token = auth_token

        # identifier, need to be set in initialize()
        self.customer_number = None

    # begin internal utility functions
    async def _request_with_retry(
        self,
        path: str,
        payload: dict | None,
        with_auth: bool = True,
        method: str = "POST",
        custom_headers: dict | None = None,
        base_path: str = BASE_PATH_APP,
    ):
        """Call _make_request, retrying one connection-establishment failure."""
        for attempt in range(2):
            try:
                return await self._make_request(
                    path, payload, with_auth, method, custom_headers, base_path
                )
            except aiohttp.ClientConnectorError as err:
                if attempt == 1:
                    raise CSGTransportError(str(err)) from err
                _LOGGER.debug(
                    "Request %s connection failed: %s, retry in 2s",
                    path,
                    err,
                )
                await asyncio.sleep(2)
            except asyncio.TimeoutError as err:
                raise CSGTransportError(str(err)) from err
            except aiohttp.ClientError as err:
                raise CSGTransportError(str(err)) from err
        raise RuntimeError("Unexpected retry loop exit")

    async def _make_request(
        self,
        path: str,
        payload: dict | None,
        with_auth: bool = True,
        method: str = "POST",
        custom_headers: dict | None = None,
        base_path: str = BASE_PATH_APP,
    ):
        """
        Function to make the http request to api endpoints
        can automatically add authentication header(s)
        """
        _LOGGER.debug(
            "CSG request: path=%s auth=%s method=%s",
            path,
            with_auth,
            method,
        )
        url = base_path + path
        headers = copy(self._common_headers)
        if custom_headers:
            for _k, _v in custom_headers.items():
                headers[_k] = _v
        if with_auth:
            headers[HEADER_X_AUTH_TOKEN] = self.auth_token
            headers[HEADER_CUST_NUMBER] = self.customer_number or ""
        if method == "POST":
            async with self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                if response.status != 200:
                    _LOGGER.error(
                        "API call %s returned status code %d", path, response.status
                    )
                    raise CSGHTTPError(response.status)

                try:
                    response_data = await response.json(content_type=None)
                except (ValueError, TypeError) as err:
                    _LOGGER.debug("response.json() failed for %s: %s", path, err)
                    json_str = (await response.read()).decode(
                        "utf-8", errors="ignore"
                    )
                    start = json_str.find("{")
                    end = json_str.rfind("}")
                    if start == -1 or end == -1 or end < start:
                        raise ValueError(
                            f"Response for {path} contains no valid JSON object"
                        ) from err
                    json_str = json_str[start : end + 1]
                    response_data = json.loads(json_str)
                if not isinstance(response_data, dict):
                    raise ValueError(
                        f"Response for {path} is not a JSON object: {type(response_data)}"
                    )
                _LOGGER.debug(
                    "CSG response: path=%s sta=%s keys=%s",
                    path,
                    response_data.get(JSON_KEY_STA),
                    sorted(response_data),
                )

                # headers need to be returned since they may contain additional data
                return response.headers, response_data

        raise NotImplementedError()

    def _handle_unsuccessful_response(self, api_path: str, response_data: dict):
        """Handles sta=!RESP_STA_SUCCESS"""
        if not isinstance(response_data, dict):
            _LOGGER.warning(
                "Invalid response_data type for %s: %s", api_path, type(response_data)
            )
            raise ValueError(
                f"response_data must be a dict for {api_path}, got {type(response_data)}"
            )
        _LOGGER.debug(
            "CSG unsuccessful response: path=%s sta=%s",
            api_path,
            response_data.get(JSON_KEY_STA),
        )
        sta = response_data.get(JSON_KEY_STA)
        msg = response_data.get(JSON_KEY_MESSAGE)
        if sta == RESP_STA_NO_LOGIN:
            raise NotLoggedIn(sta, msg)
        raise CSGAPIError(sta, msg)

    # end internal utility functions

    # begin raw api functions
    async def api_send_login_sms(self, phone_no: str):
        """Send SMS verification code to phone_no
        Note this is not the function for login with SMS, it only requests to send the code
        """
        path = "center/sendMsg"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            "phoneNumber": phone_no,
            "vcType": VERIFICATION_CODE_TYPE_LOGIN,
            "msgType": SEND_MSG_TYPE_VERIFICATION_CODE,
        }
        _, resp_data = await self._request_with_retry(path, payload, with_auth=False)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return True
        self._handle_unsuccessful_response(path, resp_data)

    async def api_create_login_qr_code(
        self, channel: QRCodeType, login_id: str | None = None
    ) -> (str, str):
        """Request API to create a QR code for login
        Returns login_id and link to QR code image
        """
        path = "center/createLoginQrcode"

        login_id = login_id or generate_qr_login_id()
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            "channel": channel,
            # NOTE: this spell error is intentional
            "lgoinId": login_id,
        }
        _, resp_data = await self._request_with_retry(
            path, payload, with_auth=False, base_path=BASE_PATH_WEB
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            image_link = resp_data.get(JSON_KEY_DATA)
            if not isinstance(image_link, str) or not image_link:
                raise CSGAPIError("INVALID_RESPONSE", "Missing QR image URL")
            return login_id, image_link
        self._handle_unsuccessful_response(path, resp_data)

    async def api_get_qr_login_status(self, login_id: str) -> tuple[bool, str]:
        """Get login status of the QR code"""
        path = "center/getLoginInfo"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            # this one is the correct spelling
            "loginId": login_id,
        }
        resp_header, resp_data = await self._request_with_retry(
            path, payload, with_auth=False, base_path=BASE_PATH_WEB
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return True, resp_header[HEADER_X_AUTH_TOKEN]
        if resp_data[JSON_KEY_STA] == RESP_STA_QR_NOT_SCANNED:
            return False, ""
        if resp_data[JSON_KEY_STA] == RESP_STA_QR_TIMEOUT:
            raise QrCodeExpired
        self._handle_unsuccessful_response(path, resp_data)

    async def api_login_with_sms_code(self, phone_no: str, sms_code: str):
        """Login with phone number and SMS code"""
        path = "center/login"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            JSON_KEY_ACCT_ID: phone_no,
            JSON_KEY_LOGON_CHAN: LOGON_CHANNEL_HANDHELD_HALL,
            JSON_KEY_CRED_TYPE: LOGIN_TYPE_PHONE_CODE,
            JSON_KEY_SMS_CODE: sms_code,
        }
        payload = {JSON_KEY_PARAM: encrypt_params(payload)}
        resp_header, resp_data = await self._request_with_retry(
            path, payload, with_auth=False, custom_headers={"need-crypto": "true"}
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_header[HEADER_X_AUTH_TOKEN]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_login_with_password_and_sms_code(
        self, phone_no: str, password: str, sms_code: str
    ):
        """Login with phone number, SMS code and password"""
        path = "center/loginByPwdAndMsg"
        payload = {
            JSON_KEY_AREA_CODE: AREACODE_FALLBACK,
            JSON_KEY_ACCT_ID: phone_no,
            JSON_KEY_LOGON_CHAN: LOGON_CHANNEL_HANDHELD_HALL,
            JSON_KEY_CRED_TYPE: LOGIN_TYPE_PHONE_PWD_CODE,
            "credentials": encrypt_credential(password),
            JSON_KEY_SMS_CODE: sms_code,
            "checkPwd": True,
        }
        payload = {JSON_KEY_PARAM: encrypt_params(payload)}
        resp_header, resp_data = await self._request_with_retry(
            path, payload, with_auth=False, custom_headers={"need-crypto": "true"}
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_header[HEADER_X_AUTH_TOKEN]
        if resp_data[JSON_KEY_STA] == RESP_STA_LOGIN_WRONG_CREDENTIAL:
            raise InvalidCredentials(
                resp_data[JSON_KEY_STA], resp_data.get(JSON_KEY_MESSAGE)
            )
        self._handle_unsuccessful_response(path, resp_data)

    async def api_query_authentication_result(self) -> dict[str, Any]:
        """Contains custNumber, used to verify login"""
        path = "user/queryAuthenticationResult"
        payload = None
        _, resp_data = await self._request_with_retry(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_get_user_info(self) -> dict[str, Any]:
        """Get account info"""
        path = "user/getUserInfo"
        payload = None
        _, resp_data = await self._request_with_retry(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_get_all_linked_electricity_accounts(
        self,
    ) -> list[dict[str, Any]]:
        """List all linked electricity accounts under this account"""
        path = "eleCustNumber/queryBindEleUsers"
        _, resp_data = await self._request_with_retry(path, {})
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            data = resp_data.get(JSON_KEY_DATA)
            if not isinstance(data, list):
                return []
            _LOGGER.debug(
                "Total %d users under this account", len(data)
            )
            return data
        self._handle_unsuccessful_response(path, resp_data)

    async def api_get_metering_point(
        self,
        area_code: str,
        ele_customer_id: str,
    ) -> dict:
        """Get metering point id"""
        path = "charge/queryMeteringPoint"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            "eleCustNumberList": [
                {JSON_KEY_ELE_CUST_ID: ele_customer_id, JSON_KEY_AREA_CODE: area_code}
            ],
        }
        # custom_headers = {"funid": "100t002"}
        custom_headers = {}
        _, resp_data = await self._request_with_retry(
            path, payload, custom_headers=custom_headers
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_query_day_electric_by_m_point(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
    ) -> dict:
        """get usage(kWh) by day in the given month"""
        path = "charge/queryDayElectricByMPoint"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
        }
        # custom_headers = {"funid": "100t002"}
        custom_headers = {}
        _, resp_data = await self._request_with_retry(
            path, payload, custom_headers=custom_headers
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_query_day_electric_charge_by_m_point(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
    ) -> dict:
        """get charge by day in the given month
        KNOWN BUG: this api call returns the daily cost data of year_month,
        but the ladder data will be this month's.
        this api call could take a long time to return (~30s)
        """
        path = "charge/queryDayElectricChargeByMPoint"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
        }
        # custom_headers = {"funid": "100t002"}  # TODO: what does this do? region?
        custom_headers = {}
        _, resp_data = await self._request_with_retry(
            path, payload, custom_headers=custom_headers
        )
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_query_account_surplus(
        self, area_code: str, ele_customer_id: str
    ):
        """Contains: balance and arrears"""
        path = "charge/queryUserAccountNumberSurplus"
        payload = {JSON_KEY_AREA_CODE: area_code, JSON_KEY_ELE_CUST_ID: ele_customer_id}
        _, resp_data = await self._request_with_retry(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_get_fee_analyze_details(
        self, year: int, area_code: str, ele_customer_id: str
    ):
        """
        Contains: year total kWh, year total charge, kWh/charge by month in current year
        """
        path = "charge/getAnalyzeFeeDetails"
        payload = {
            JSON_KEY_AREA_CODE: area_code,
            "electricityBillYear": year,
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_METERING_POINT_ID: None,  # this is set to null in api
        }
        _, resp_data = await self._request_with_retry(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_query_day_electric_by_m_point_yesterday(
        self,
        area_code: str,
        ele_customer_id: str,
    ) -> dict:
        """Contains: power consumption(kWh) of yesterday"""
        path = "charge/queryDayElectricByMPointYesterday"
        payload = {JSON_KEY_ELE_CUST_ID: ele_customer_id, JSON_KEY_AREA_CODE: area_code}
        _, resp_data = await self._request_with_retry(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_query_electricity_calendar(
        self,
        year: int,
        month: int,
        area_code: str,
        ele_customer_id: str,
        metering_point_id: str,
        metering_point_number: str,
    ) -> dict:
        """Shenzhen electricity calendar: daily usage + temperatures.

        The app's Shenzhen "electricity calendar" endpoint. Returns
        totalPower (month total) and result (per-day power/temperature).
        """
        path = "charge/queryElectricityCalendar"
        payload = {
            JSON_KEY_ELE_CUST_ID: ele_customer_id,
            JSON_KEY_AREA_CODE: area_code,
            JSON_KEY_YEAR_MONTH: f"{year}{month:02d}",
            JSON_KEY_METERING_POINT_ID: metering_point_id,
            "deviceIdentif": metering_point_number,
        }
        _, resp_data = await self._request_with_retry(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    async def api_logout(self, logon_chan: str, cred_type: LoginType) -> None:
        """logout"""
        path = "center/logout"
        payload = {JSON_KEY_LOGON_CHAN: logon_chan, JSON_KEY_CRED_TYPE: cred_type}
        _, resp_data = await self._request_with_retry(path, payload)
        if resp_data[JSON_KEY_STA] == RESP_STA_SUCCESS:
            return resp_data[JSON_KEY_DATA]
        self._handle_unsuccessful_response(path, resp_data)

    # end raw api functions

    # begin utility functions
    @staticmethod
    def load(
        data: dict[str, str], session: aiohttp.ClientSession
    ) -> CSGClient:
        """
        Restore the session info to client object
        The validity of the session won't be checked
        `initialize()` needs to be called for the client to be usable
        """
        for k in (ATTR_AUTH_TOKEN,):
            if not data.get(k):
                raise ValueError(f"missing parameter: {k}")
        client = CSGClient(
            session=session,
            auth_token=data[ATTR_AUTH_TOKEN],
        )
        return client

    def dump(self) -> dict[str, Any]:
        """Dump the session to dict"""
        return {
            ATTR_AUTH_TOKEN: self.auth_token,
        }

    def set_authentication_params(self, auth_token: str):
        """Set self.auth_token and client generated cookies"""
        self.auth_token = auth_token

    async def initialize(self):
        """Initialize the client"""
        resp_data = await self.api_get_user_info()
        if not isinstance(resp_data, dict) or not resp_data.get(JSON_KEY_CUST_NUMBER):
            raise CSGAPIError("INVALID_RESPONSE", "Missing customer number")
        self.customer_number = resp_data[JSON_KEY_CUST_NUMBER]

    async def verify_login(self) -> bool:
        """Verify validity of the session"""
        try:
            await self.api_query_authentication_result()
        except NotLoggedIn:
            return False
        return True

    async def logout(self, login_type: LoginType):
        """Logout and reset identifier, token etc."""
        await self.api_logout(LOGON_CHANNEL_HANDHELD_HALL, login_type)
        self.auth_token = None
        self.customer_number = None

    # end utility functions

    # begin high-level api wrappers

    async def get_all_electricity_accounts(self) -> list[CSGElectricityAccount]:
        """Get all electricity accounts linked to current account"""
        result = []
        ele_user_resp_data = await self.api_get_all_linked_electricity_accounts()

        for item in ele_user_resp_data:
            if not isinstance(item, dict):
                continue
            required_account_keys = (
                JSON_KEY_AREA_CODE,
                "bindingId",
                "eleCustNumber",
                "eleAddress",
                "userName",
            )
            if any(item.get(key) is None for key in required_account_keys):
                _LOGGER.warning(
                    "Skipping malformed linked electricity account; keys=%s",
                    sorted(item),
                )
                continue
            try:
                metering_point_data = await self.api_get_metering_point(
                    item[JSON_KEY_AREA_CODE], item["bindingId"]
                )
            except CSGAPIError as err:
                if err.sta != RESP_STA_NO_METERING_POINT:
                    raise
                _LOGGER.warning(
                    "Skipping linked electricity account without a metering point"
                )
                continue
            if not isinstance(metering_point_data, list) or not metering_point_data:
                _LOGGER.warning(
                    "Skipping linked electricity account with empty metering data"
                )
                continue
            metering_point = metering_point_data[0]
            if not isinstance(metering_point, dict):
                continue
            metering_point_id = metering_point.get(JSON_KEY_METERING_POINT_ID)
            metering_point_number = metering_point.get(JSON_KEY_METERING_POINT_NUMBER)
            if metering_point_id is None or metering_point_number is None:
                _LOGGER.warning(
                    "Skipping linked electricity account with malformed metering data"
                )
                continue
            account = CSGElectricityAccount(
                account_number=item["eleCustNumber"],
                area_code=item[JSON_KEY_AREA_CODE],
                ele_customer_id=item["bindingId"],
                metering_point_id=metering_point_id,
                metering_point_number=metering_point_number,
                address=item["eleAddress"],
                user_name=item["userName"],
            )
            result.append(account)
        return result

    async def get_month_daily_usage_detail(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> tuple[float, list[dict[str, str | float]]]:
        """Get daily usage of current month"""

        year, month = year_month

        resp_data = await self.api_query_day_electric_by_m_point(
            year,
            month,
            account.area_code,
            account.ele_customer_id,
            account.metering_point_id,
        )
        if not isinstance(resp_data, dict):
            return 0.0, []
        total_power = resp_data.get("totalPower")
        month_total_kwh = float(total_power) if total_power is not None else 0.0
        by_day = []
        result = resp_data.get("result")
        for d_data in result if isinstance(result, list) else []:
            if not isinstance(d_data, dict):
                continue
            if d_data.get("date") is None or d_data.get("power") is None:
                continue
            by_day.append(
                {WF_ATTR_DATE: d_data["date"], WF_ATTR_KWH: float(d_data["power"])}
            )
        return month_total_kwh, by_day

    async def get_month_daily_cost_detail(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> tuple[float | None, float | None, dict, list[dict[str, str | float]]]:
        """Get daily cost of current month"""

        year, month = year_month

        resp_data = await self.api_query_day_electric_charge_by_m_point(
            year,
            month,
            account.area_code,
            account.ele_customer_id,
            account.metering_point_id,
        )

        if not isinstance(resp_data, dict):
            return None, None, {}, []
        by_day = []
        result = resp_data.get("result")
        for d_data in result if isinstance(result, list) else []:
            if not isinstance(d_data, dict):
                continue
            if any(d_data.get(key) is None for key in ("date", "charge", "power")):
                continue
            by_day.append(
                {
                    WF_ATTR_DATE: d_data["date"],
                    WF_ATTR_CHARGE: float(d_data["charge"]),
                    WF_ATTR_KWH: float(d_data["power"]),
                }
            )

        # sometimes the data by day is present, but the total amount and ladder are not

        if resp_data.get("totalElectricity") is not None:
            month_total_cost = float(resp_data["totalElectricity"])
        else:
            month_total_cost = None

        if resp_data.get("totalPower") is not None:
            month_total_kwh = float(resp_data["totalPower"])
        else:
            month_total_kwh = None

        # sometimes the ladder info is null, handle that
        if resp_data.get("ladderEle") is not None:
            current_ladder = int(resp_data["ladderEle"])
        else:
            current_ladder = None
        # API 返回格式可能是 "2023-05-01 00:00:00.0" 或 "2023-05-01 00:00:00"
        if resp_data.get("ladderEleStartDate") is not None:
            date_str = resp_data["ladderEleStartDate"]
            try:
                # 先尝试带毫秒的格式
                current_ladder_start_date = datetime.datetime.strptime(
                    date_str, "%Y-%m-%d %H:%M:%S.%f"
                )
            except ValueError:
                try:
                    # 再尝试不带毫秒的格式
                    current_ladder_start_date = datetime.datetime.strptime(
                        date_str, "%Y-%m-%d %H:%M:%S"
                    )
                except ValueError as e:
                    _LOGGER.warning(
                        "Failed to parse ladder start date '%s': %s",
                        date_str,
                        e,
                    )
                    current_ladder_start_date = None
        else:
            current_ladder_start_date = None
        if resp_data.get("ladderEleSurplus") is not None:
            current_ladder_remaining_kwh = float(resp_data["ladderEleSurplus"])
        else:
            current_ladder_remaining_kwh = None
        if resp_data.get("ladderEleTariff") is not None:
            current_tariff = float(resp_data["ladderEleTariff"])
        else:
            current_tariff = None
        # TODO what will happen to `current_ladder_remaining_kwh` when it's the last ladder?
        ladder = {
            WF_ATTR_LADDER: current_ladder,
            WF_ATTR_LADDER_START_DATE: current_ladder_start_date,
            WF_ATTR_LADDER_REMAINING_KWH: current_ladder_remaining_kwh,
            WF_ATTR_LADDER_TARIFF: current_tariff,
        }

        return month_total_cost, month_total_kwh, ladder, by_day

    async def get_balance_and_arrears(
        self, account: CSGElectricityAccount
    ) -> tuple[float, float]:
        """Get account balance and arrears"""

        resp_data = await self.api_query_account_surplus(
            account.area_code, account.ele_customer_id
        )
        if not isinstance(resp_data, list) or not resp_data:
            return 0.0, 0.0
        if not isinstance(resp_data[0], dict):
            return 0.0, 0.0
        balance = resp_data[0].get("balance") or 0
        arrears = resp_data[0].get("arrears") or 0
        return float(balance), float(arrears)

    async def get_year_month_stats(
        self, account: CSGElectricityAccount, year
    ) -> tuple[float, float, list[dict[str, str | float]]]:
        """Get year total kWh, year total charge, kWh/charge by month in current year"""

        resp_data = await self.api_get_fee_analyze_details(
            year, account.area_code, account.ele_customer_id
        )
        if not isinstance(resp_data, dict):
            return 0.0, 0.0, []
        total_year_kwh = resp_data.get("totalBillingElectricity") or 0
        total_year_charge = resp_data.get("totalActualAmount") or 0
        by_month = []
        monthly_data = resp_data.get("electricAndChargeList")
        for m_data in monthly_data if isinstance(monthly_data, list) else []:
            if not isinstance(m_data, dict):
                continue
            if any(
                m_data.get(key) is None
                for key in (
                    JSON_KEY_YEAR_MONTH,
                    "actualTotalAmount",
                    "billingElectricity",
                )
            ):
                continue
            by_month.append(
                {
                    WF_ATTR_MONTH: m_data[JSON_KEY_YEAR_MONTH],
                    WF_ATTR_CHARGE: float(m_data["actualTotalAmount"]),
                    WF_ATTR_KWH: float(m_data["billingElectricity"]),
                }
            )
        return float(total_year_charge), float(total_year_kwh), by_month

    async def get_yesterday_kwh(
        self, account: CSGElectricityAccount
    ) -> float | None:
        """Get power consumption(kwh) of yesterday"""
        resp_data = await self.api_query_day_electric_by_m_point_yesterday(
            account.area_code, account.ele_customer_id
        )
        if isinstance(resp_data, dict) and resp_data.get("power") is not None:
            return float(resp_data["power"])
        return None

    async def get_month_daily_usage_detail_sz(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> tuple[float, list[dict[str, str | float]]]:
        """Get daily usage of current month via the Shenzhen electricity
        calendar endpoint (queryElectricityCalendar).

        The legacy queryDayElectricByMPoint endpoint no longer returns data
        for Shenzhen (area code 090000) accounts since the CSG server-side
        migration; the app uses this calendar endpoint instead.
        """
        year, month = year_month
        resp_data = await self.api_query_electricity_calendar(
            year,
            month,
            account.area_code,
            account.ele_customer_id,
            account.metering_point_id,
            account.metering_point_number,
        )
        if not isinstance(resp_data, dict):
            return 0.0, []
        total_power = resp_data.get("totalPower")
        month_total_kwh = float(total_power) if total_power is not None else 0.0
        by_day = []
        result = resp_data.get("result")
        for d_data in result if isinstance(result, list) else []:
            if not isinstance(d_data, dict):
                continue
            if d_data.get("date") is None or d_data.get("power") is None:
                continue
            by_day.append(
                {WF_ATTR_DATE: d_data["date"], WF_ATTR_KWH: float(d_data["power"])}
            )
        return month_total_kwh, by_day

    async def get_month_bill_list(
        self, account: CSGElectricityAccount, year_month: tuple[int, int]
    ) -> dict | None:
        """Get the monthly electricity bill overview (selectElecBillList).

        Returns the first bill overview dict for the given year-month, or
        None when no bill has been generated yet (current month is billed
        after month close).
        """
        year, month = year_month
        payload = {
            "areaCode": "",
            "yearMonth": f"{year}{month:02d}",
            "eleCustIdList": [{"eleCustId": account.ele_customer_id}],
        }
        _, resp_data = await self._request_with_retry(
            "charge/selectElecBillList",
            {JSON_KEY_PARAM: encrypt_params(payload)},
            custom_headers={"need-crypto": "true"},
        )
        if resp_data[JSON_KEY_STA] != RESP_STA_SUCCESS:
            self._handle_unsuccessful_response("charge/selectElecBillList", resp_data)
        data = resp_data.get(JSON_KEY_DATA)
        if isinstance(data, str):
            data = decrypt_params(data)
        if not isinstance(data, dict):
            return None
        bills = data.get("billOverviewModelList")
        if isinstance(bills, list) and bills:
            return bills[0]
        return None

    # end high-level api wrappers
