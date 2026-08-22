"""Tests for usp.fetcher.async_client — retry, backoff, error surfacing."""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from usp.fetcher.async_client import (
    RETRYABLE_EXCEPTIONS,
    RETRYABLE_STATUSES,
    AsyncWebClient,
    _AsyncResponse,
    _backoff_sleep,
    _parse_retry_after,
)


# --- _backoff_sleep() ---------------------------------------------------

def test_backoff_no_wait_on_attempt_zero():
    assert _backoff_sleep(0, base=0.5, cap=10.0) == 0.0


def test_backoff_grows_exponentially():
    # 100 trials across all attempts, all values should be in the
    # expected ranges.
    for attempt in (1, 2, 3):
        for _ in range(100):
            v = _backoff_sleep(attempt, base=0.5, cap=10.0)
            upper = min(10.0, 0.5 * (2 ** (attempt - 1)))
            assert 0.0 <= v <= upper + 1e-9


def test_backoff_caps_at_max():
    for _ in range(100):
        v = _backoff_sleep(20, base=0.5, cap=5.0)
        assert v <= 5.0 + 1e-9


# --- _parse_retry_after() ----------------------------------------------

def test_retry_after_empty():
    assert _parse_retry_after(None) == 0.0
    assert _parse_retry_after("") == 0.0


def test_retry_after_delta_seconds():
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("  10  ") == 10.0


def test_retry_after_unparseable_garbage():
    # Garbage that is not delta-seconds nor HTTP-date returns 0.
    assert _parse_retry_after("not-a-date") == 0.0


# --- AsyncWebClient.get() retry semantics -------------------------------

def _mk_response(
    *,
    status: int = 200,
    url: str = "https://x/",
    body: bytes = b"<ok/>",
    headers: dict | None = None,
) -> "_AsyncResponse":
    return _AsyncResponse(
        url=url, status_code=status, data=body, headers=headers or {}
    )


def _mk_httpx_response(
    *,
    status_code: int = 200,
    url: str = "https://x/",
    content: bytes = b"<ok/>",
    body: bytes | None = None,
    headers: dict | None = None,
) -> httpx.Response:
    if body is not None:
        content = body
    r = httpx.Response(status_code=status_code, content=content, request=httpx.Request("GET", url))
    if headers:
        r.headers.update(headers)
    return r


@pytest.mark.asyncio
async def test_returns_2xx_response_directly(monkeypatch):
    """A single 200 should not trigger any retry."""
    cli = AsyncWebClient(max_attempts=4)
    fake = _mk_httpx_response(status_code=200)
    monkeypatch.setattr(cli._client, "get", AsyncMock(return_value=fake))
    resp = await cli.get("https://x/feed")
    assert resp.status_code() == 200
    assert resp.error is None
    assert cli._client.get.await_count == 1
    await cli.aclose()


@pytest.mark.asyncio
async def test_retries_5xx_then_succeeds(monkeypatch):
    """Two 503s followed by a 200 → should be returned as success after 3 calls."""
    cli = AsyncWebClient(max_attempts=4, backoff_base=0.001, backoff_cap=0.001)
    fake = _mk_httpx_response(status_code=503)
    ok = _mk_httpx_response(status_code=200, body=b"final")
    monkeypatch.setattr(cli._client, "get", AsyncMock(side_effect=[fake, fake, ok]))
    resp = await cli.get("https://x/feed")
    assert resp.status_code() == 200
    assert cli._client.get.await_count == 3
    await cli.aclose()


@pytest.mark.asyncio
async def test_retries_then_gives_up_after_max_attempts(monkeypatch):
    cli = AsyncWebClient(max_attempts=3, backoff_base=0.001, backoff_cap=0.001)
    fake = _mk_httpx_response(status_code=500)
    monkeypatch.setattr(cli._client, "get", AsyncMock(return_value=fake))
    resp = await cli.get("https://x/feed")
    assert resp.status_code() == 500
    assert cli._client.get.await_count == 3  # tried 3 times
    await cli.aclose()


@pytest.mark.asyncio
async def test_does_not_retry_4xx(monkeypatch):
    """404 is not in the retryable set → single attempt only."""
    cli = AsyncWebClient(max_attempts=4)
    fake = _mk_httpx_response(status_code=404)
    monkeypatch.setattr(cli._client, "get", AsyncMock(return_value=fake))
    resp = await cli.get("https://x/missing")
    assert resp.status_code() == 404
    assert cli._client.get.await_count == 1
    await cli.aclose()


@pytest.mark.asyncio
async def test_retries_429_with_retry_after(monkeypatch):
    """429 with Retry-After: 0.01 → second attempt immediately."""
    cli = AsyncWebClient(max_attempts=3, backoff_base=0.001, backoff_cap=0.001)
    r429 = _mk_httpx_response(
        status_code=429,
        headers={"Retry-After": "0.01"},
    )
    ok = _mk_httpx_response(status_code=200, body=b"x")
    monkeypatch.setattr(cli._client, "get", AsyncMock(side_effect=[r429, ok]))
    t0 = time.monotonic()
    resp = await cli.get("https://x/feed")
    elapsed = time.monotonic() - t0
    assert resp.status_code() == 200
    assert cli._client.get.await_count == 2
    # Slept for the Retry-After interval.
    assert elapsed >= 0.005
    await cli.aclose()


@pytest.mark.asyncio
async def test_retries_on_connect_error(monkeypatch):
    """A single ConnectError should be retried; second attempt succeeds."""
    cli = AsyncWebClient(max_attempts=3, backoff_base=0.001, backoff_cap=0.001)
    ok = _mk_httpx_response(status_code=200)
    monkeypatch.setattr(
        cli._client, "get",
        AsyncMock(side_effect=[httpx.ConnectError("boom"), ok]),
    )
    resp = await cli.get("https://x/feed")
    assert resp.status_code() == 200
    assert cli._client.get.await_count == 2
    await cli.aclose()


@pytest.mark.asyncio
async def test_non_retryable_exception_surfaces_immediately(monkeypatch):
    """ValueError is not in RETRYABLE_EXCEPTIONS → surface right away."""
    cli = AsyncWebClient(max_attempts=4)
    monkeypatch.setattr(
        cli._client, "get",
        AsyncMock(side_effect=ValueError("oops")),
    )
    resp = await cli.get("https://x/feed")
    assert resp.status_code() == 0
    assert isinstance(resp.error, ValueError)
    assert cli._client.get.await_count == 1
    await cli.aclose()


@pytest.mark.asyncio
async def test_status_codes_classification():
    """Spot-check the retryable status set."""
    for code in (408, 425, 429, 500, 502, 503, 504, 507):
        assert code in RETRYABLE_STATUSES, f"{code} should be retryable"
    for code in (200, 301, 400, 401, 403, 404, 410, 501):
        assert code not in RETRYABLE_STATUSES, f"{code} should NOT be retryable"


@pytest.mark.asyncio
async def test_exception_types_classification():
    """Spot-check the retryable exception set."""
    for cls in (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                httpx.ReadTimeout, httpx.WriteError, httpx.PoolTimeout,
                httpx.RemoteProtocolError, httpx.CloseError):
        assert cls in RETRYABLE_EXCEPTIONS, f"{cls.__name__} should be retryable"


@pytest.mark.asyncio
async def test_max_data_length_truncates_body(monkeypatch):
    """A max_response_data_length caps the body size of the returned response."""
    cli = AsyncWebClient(max_attempts=1, backoff_base=0.001)
    cli.set_max_response_data_length(5)
    fake = _mk_httpx_response(status_code=200, content=b"hello world")
    monkeypatch.setattr(cli._client, "get", AsyncMock(return_value=fake))
    resp = await cli.get("https://x/feed")
    assert resp.status_code() == 200
    assert resp.raw_data() == b"hello"
    await cli.aclose()


@pytest.mark.asyncio
async def test_final_url_is_returned(monkeypatch):
    """After redirects, resp.url() is the final URL."""
    cli = AsyncWebClient(max_attempts=1)
    fake = _mk_httpx_response(
        status_code=200, url="https://final/x"
    )
    monkeypatch.setattr(cli._client, "get", AsyncMock(return_value=fake))
    resp = await cli.get("https://original/x")
    assert resp.url() == "https://final/x"
    await cli.aclose()


# --- _AsyncResponse ----------------------------------------------------

def test_response_from_error_sets_zero_status():
    r = _AsyncResponse.from_error("https://x", ConnectionError("oops"))
    assert r.status_code() == 0
    assert r.error is not None
    assert r.raw_data() == b""


def test_response_header_lookup_is_case_insensitive():
    r = _AsyncResponse(
        url="https://x",
        status_code=200,
        data=b"",
        headers={"Content-Type": "text/plain"},
    )
    assert r.header("content-type") == "text/plain"
    assert r.header("CONTENT-TYPE") == "text/plain"
    assert r.header("x-not-here") is None
