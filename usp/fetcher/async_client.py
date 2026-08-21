"""Async web client built on httpx.

Provides a drop-in for ``AbstractWebClient`` but supports async HTTP/2
multiplexing via httpx. Used by ``usp.fetcher.crawler.AsyncCrawler``.
"""

from __future__ import annotations

import gzip
import io
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "usp-async/0.x (+https://github.com/GateNLP/ultimate-sitemap-parser)"


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


class AsyncWebClient:
    """Async httpx-backed web client.

    Returns a small wrapper exposing ``status_code()``, ``raw_data()``,
    ``header(name)``, ``url()`` so it can be used by the sync fetch_parse
    code if needed. The async API is :meth:`get`.
    """

    __slots__ = ("_client", "_max_data_length", "user_agent")

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
    ):
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self._client = httpx.AsyncClient(
            http2=http2,
            timeout=httpx.Timeout(timeout[1], connect=timeout[0]),
            verify=verify,
            limits=limits,
            follow_redirects=follow_redirects,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"},
        )
        self._max_data_length: int | None = None
        self.user_agent = user_agent

    def set_max_response_data_length(self, n: int | None) -> None:
        self._max_data_length = n

    async def get(self, url: str) -> _AsyncResponse:
        """Fetch URL. Returns :class:`_AsyncResponse` even on HTTP errors."""
        try:
            resp = await self._client.get(url, headers={"User-Agent": self.user_agent})
        except Exception as ex:
            return _AsyncResponse.from_error(url, ex)
        if self._max_data_length is not None:
            data = resp.content[: self._max_data_length]
        else:
            data = resp.content
        return _AsyncResponse(
            url=str(resp.url),
            status_code=resp.status_code,
            data=data,
            headers=dict(resp.headers),
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

    def __init__(self, *, url: str, status_code: int, data: bytes, headers: dict):
        self._url = url
        self._status_code = status_code
        self._data = data
        self._headers = headers
        self._error: Exception | None = None

    @classmethod
    def from_error(cls, url: str, error: Exception) -> _AsyncResponse:
        r = cls(url=url, status_code=0, data=b"", headers={})
        r._error = error
        return r

    def status_code(self) -> int:
        return self._status_code

    def raw_data(self) -> bytes:
        return self._data

    def header(self, name: str) -> str | None:
        return self._headers.get(name.lower())

    def url(self) -> str:
        return self._url

    @property
    def error(self) -> Exception | None:
        return self._error
