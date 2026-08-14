"""Tests for the aiohttp transport used by the CSG client."""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from collections import namedtuple
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

sys.path.insert(
    0,
    str(
        Path(__file__).parents[1]
        / "custom_components"
        / "china_southern_power_grid_stat"
    ),
)

from csg_client import (  # noqa: E402
    CSGClient,
    CSGHTTPError,
    CSGTransportError,
)


class FakeResponse:
    """Small aiohttp response double."""

    def __init__(
        self,
        *,
        status: int = 200,
        data: object | None = None,
        raw: bytes = b"",
        json_error: Exception | None = None,
        enter_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.headers = {"x-auth-token": "token"}
        self._data = data
        self._raw = raw
        self._json_error = json_error
        self._enter_error = enter_error

    async def __aenter__(self):
        if self._enter_error is not None:
            raise self._enter_error
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, *, content_type=None):
        assert content_type is None
        if self._json_error is not None:
            raise self._json_error
        return self._data

    async def read(self) -> bytes:
        return self._raw


class FakeSession:
    """Small aiohttp session double with queued responses."""

    def __init__(self, *responses: FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._responses.pop(0)


def connector_error(message: str = "unreachable") -> aiohttp.ClientConnectorError:
    key_type = namedtuple("ConnectionKey", "host port ssl")
    return aiohttp.ClientConnectorError(
        key_type("95598.csg.cn", 443, True), OSError(message)
    )


@pytest.mark.asyncio
async def test_make_request_uses_bounded_timeout_and_preserves_request_shape():
    session = FakeSession(FakeResponse(data={"sta": "00", "data": {}}))
    client = CSGClient(session, auth_token="auth")
    client.customer_number = "customer"

    headers, data = await client._make_request("probe", {"value": 1})

    assert headers["x-auth-token"] == "token"
    assert data == {"sta": "00", "data": {}}
    assert session.calls[0]["url"].endswith("/probe")
    assert session.calls[0]["json"] == {"value": 1}
    assert session.calls[0]["headers"]["x-auth-token"] == "auth"
    assert session.calls[0]["headers"]["custNumber"] == "customer"
    timeout = session.calls[0]["timeout"]
    assert timeout.total == 40
    assert timeout.connect == 5
    assert timeout.sock_connect == 5
    assert timeout.sock_read == 30


@pytest.mark.asyncio
async def test_request_before_initialize_uses_empty_customer_number():
    session = FakeSession(FakeResponse(data={"sta": "00"}))
    client = CSGClient(session, auth_token="auth")

    await client._make_request("probe", {})

    assert session.calls[0]["headers"]["custNumber"] == ""


@pytest.mark.asyncio
async def test_request_retries_one_connector_error(monkeypatch):
    session = FakeSession(
        FakeResponse(enter_error=connector_error()),
        FakeResponse(data={"sta": "00"}),
    )
    client = CSGClient(session)

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    _, data = await client._request_with_retry("probe", {})

    assert data == {"sta": "00"}
    assert len(session.calls) == 2
    assert sleeps == [2]


@pytest.mark.asyncio
async def test_timeout_is_not_retried():
    session = FakeSession(
        FakeResponse(enter_error=aiohttp.ServerTimeoutError("slow")),
        FakeResponse(data={"sta": "00"}),
    )
    client = CSGClient(session)

    with pytest.raises(CSGTransportError) as exc_info:
        await client._request_with_retry("probe", {})

    assert "slow" in str(exc_info.value)
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_total_timeout_is_not_retried():
    session = FakeSession(
        FakeResponse(enter_error=TimeoutError("total timeout")),
        FakeResponse(data={"sta": "00"}),
    )
    client = CSGClient(session)

    with pytest.raises(CSGTransportError):
        await client._request_with_retry("probe", {})

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_http_error_is_not_retried():
    session = FakeSession(
        FakeResponse(status=503),
        FakeResponse(data={"sta": "00"}),
    )
    client = CSGClient(session)

    with pytest.raises(CSGHTTPError):
        await client._request_with_retry("probe", {})

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_non_json_prefix_fallback_is_preserved():
    session = FakeSession(
        FakeResponse(
            json_error=ValueError("invalid json"),
            raw=b'prefix {"sta": "00", "data": {}} suffix',
        )
    )
    client = CSGClient(session)

    _, data = await client._make_request("probe", {})

    assert data == {"sta": "00", "data": {}}


def test_load_reuses_injected_session():
    session = FakeSession()

    client = CSGClient.load({"auth_token": "token"}, session)

    assert client._session is session
    assert client.auth_token == "token"


@pytest.mark.asyncio
async def test_real_ipv4_session_timeout_cancels_request():
    async def slow_handler(_request):
        await asyncio.sleep(1)
        return web.json_response({"sta": "00"})

    app = web.Application()
    app.router.add_post("/probe", slow_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        assert connector._family == socket.AF_INET
        client = CSGClient(session, auth_token="auth")
        client._timeout = aiohttp.ClientTimeout(total=0.05)
        started = time.monotonic()

        with pytest.raises(CSGTransportError):
            await client._request_with_retry(
                "probe", {}, base_path=f"http://127.0.0.1:{port}/"
            )

        assert time.monotonic() - started < 0.5

    await runner.cleanup()
