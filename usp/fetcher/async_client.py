"""Async web client built on httpx with optional retry + backoff.

Provides a drop-in for :class:`usp.web_client.abstract_client.AbstractWebClient`
but supports async HTTP/2 multiplexing via httpx. Used by
:mod:`usp.fetcher.crawler`.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import logging
import random
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "usp-async/0.x (+https://github.com/GateNLP/ultimate-sitemap-parser)"

# Status codes worth retrying on.
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 507})

# Network exceptions worth retrying on.
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.LocalProtocolError,
    httpx.CloseError,
)


def _looks_like_gzip(url: str, content_type: str | None, data: bytes) -> bool:
    if url.lower().endswith(".gz"):
        return True
    if content_type and "gzip" in content_type.lower():
        return True
    return data[:2] == b"\x1f\x8b"


def gunzip(data: bytes, max_output_bytes: int | None = None) -> bytes:
    """Decompress a gzip stream in chunks, enforcing ``max_output_bytes``."""
    if data is None or not data:
        raise ValueError("empty data")
    chunks: list[bytes] = []
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        while True:
            chunk = gz.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_output_bytes is not None and total > max_output_bytes:
                raise ValueError(f"gunzipped > {max_output_bytes}")
            chunks.append(chunk)
    return b"".join(chunks)


def ungzipped_bytes(
    url: str,
    content: bytes,
    content_type: str | None,
    max_uncompressed_bytes: int | None = None,
) -> bytes:
    """Return decompressed bytes, gunzipping if necessary.

    Unlike :func:`usp.helpers.ungzipped_response_content`, this returns
    raw bytes instead of str. Sitemaps are XML (utf-8); expat accepts bytes.
    """
    if _looks_like_gzip(url, content_type, content):
        try:
            return gunzip(content, max_output_bytes=max_uncompressed_bytes)
        except Exception as ex:
            log.warning("gunzip failed for %s, treating as raw: %s", url, ex)
    return content


def _backoff_sleep(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter.

    attempt=0 -> no extra wait (just the base)
    attempt=1 -> [0, base)
    attempt=2 -> [0, 2*base)
    attempt=3 -> [0, 4*base) capped at ``cap``
    """
    if attempt <= 0:
        return 0.0
    upper = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0.0, upper)


def _parse_retry_after(value: str | None) -> float:
    """Parse ``Retry-After`` header. Returns seconds (>=0) or 0."""
    if not value:
        return 0.0
    value = value.strip()
    try:
        # delta-seconds form
        seconds = float(value)
        return max(0.0, seconds)
    except ValueError:
        pass
    # HTTP-date form (best effort)
    from email.utils import parsedate_to_datetime
    try:
        target = parsedate_to_datetime(value)
        now = parsedate_to_datetime(None) if False else None
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        delta = (target - now).total_seconds()
        return max(0.0, delta)
    except Exception:
        return 0.0


class AsyncWebClient:
    """Async httpx-backed web client with built-in retry + backoff.

    The :meth:`get` method retries on:

    * retryable HTTP status codes (default: 408/425/429/500/502/503/504/507)
    * retryable network exceptions (default: connection / read / write timeouts,
      protocol errors, connection resets)

    Retries use exponential backoff with full jitter, capped at
    ``max_backoff`` seconds. The ``Retry-After`` response header is honoured
    (clamped to ``max_retry_after``) on 429 / 503 responses.
    """

    __slots__ = (
        "_client",
        "_max_data_length",
        "user_agent",
        "_max_attempts",
        "_backoff_base",
        "_backoff_cap",
        "_max_retry_after",
        "_proxy",
    )

    def __init__(
        self,
        *,
        http2: bool = True,
        timeout: tuple[float, float] = (9.05, 60.0),
        verify: bool = True,
        max_connections: int = 20,
        max_keepalive_connections: int = 20,
        user_agent: str = USER_AGENT,
        follow_redirects: bool = True,
        max_attempts: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        max_retry_after: float = 60.0,
        proxy: str | None = None,
    ):
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self._proxy = proxy or None
        self._client = httpx.AsyncClient(
            http2=http2,
            timeout=httpx.Timeout(timeout[1], connect=timeout[0]),
            verify=verify,
            limits=limits,
            follow_redirects=follow_redirects,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"},
            proxy=self._proxy,
        )
        self._max_data_length: int | None = None
        self.user_agent = user_agent
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._max_retry_after = max_retry_after

    @property
    def proxy(self) -> str | None:
        """Return the configured proxy URL (``None`` for direct connect)."""
        return self._proxy

    def set_max_response_data_length(self, n: int | None) -> None:
        self._max_data_length = n

    async def get(self, url: str) -> _AsyncResponse:
        """Fetch URL with retry. Returns :class:`_AsyncResponse` even on errors.

        On any non-retryable error, the returned response has
        ``status_code=0`` and ``error=<exception>``.
        """
        last_exc: BaseException | None = None
        last_status: int = 0

        for attempt in range(self._max_attempts):
            try:
                resp = await self._client.get(
                    url, headers={"User-Agent": self.user_agent}
                )
            except RETRYABLE_EXCEPTIONS as ex:
                last_exc = ex
                last_status = 0
                if attempt + 1 >= self._max_attempts:
                    break
                sleep_for = _backoff_sleep(attempt, self._backoff_base, self._backoff_cap)
                log.warning(
                    "fetch %s failed (attempt %d/%d): %s; retrying in %.2fs",
                    url, attempt + 1, self._max_attempts, ex, sleep_for,
                )
                await asyncio.sleep(sleep_for)
                continue
            except Exception as ex:
                # Non-retryable; surface immediately.
                return _AsyncResponse.from_error(url, ex, status=0)

            status = resp.status_code
            if status in RETRYABLE_STATUSES and attempt + 1 < self._max_attempts:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                retry_after = min(retry_after, self._max_retry_after)
                sleep_for = max(
                    _backoff_sleep(attempt, self._backoff_base, self._backoff_cap),
                    retry_after,
                )
                log.warning(
                    "fetch %s returned %d (attempt %d/%d); retrying in %.2fs",
                    url, status, attempt + 1, self._max_attempts, sleep_for,
                )
                await resp.aclose()
                await asyncio.sleep(sleep_for)
                last_status = status
                continue

            # Truncate body if requested.
            if self._max_data_length is not None:
                data = resp.content[: self._max_data_length]
            else:
                data = resp.content

            return _AsyncResponse(
                url=str(resp.url),
                status_code=status,
                data=data,
                headers=dict(resp.headers),
            )

        # Exhausted retries.
        if last_exc is not None:
            return _AsyncResponse.from_error(url, last_exc, status=0)
        return _AsyncResponse(
            url=url, status_code=last_status, data=b"", headers={}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncWebClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


class _AsyncResponse:
    """Minimal response wrapper compatible with the sync AbstractWebClient."""

    __slots__ = ("_url", "_status_code", "_data", "_headers", "_error")

    def __init__(
        self,
        *,
        url: str,
        status_code: int,
        data: bytes,
        headers: dict,
    ):
        self._url = url
        self._status_code = status_code
        self._data = data
        self._headers = headers
        self._error: BaseException | None = None

    @classmethod
    def from_error(
        cls, url: str, error: BaseException, *, status: int = 0
    ) -> _AsyncResponse:
        r = cls(url=url, status_code=status, data=b"", headers={})
        r._error = error
        return r

    def status_code(self) -> int:
        return self._status_code

    def raw_data(self) -> bytes:
        return self._data

    def header(self, name: str) -> str | None:
        # ``httpx.Headers`` is itself case-insensitive, so this works
        # for both plain-dict and ``Headers`` inputs.
        h = self._headers.get(name)
        if h is not None:
            return h
        # Fallback: case-insensitive scan of plain dicts.
        lname = name.lower()
        for k, v in self._headers.items():
            if k.lower() == lname:
                return v
        return None

    def url(self) -> str:
        return self._url

    @property
    def error(self) -> BaseException | None:
        return self._error
