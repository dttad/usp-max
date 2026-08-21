"""Async concurrent sitemap crawler using a flat worker-pool design.

Architecture:
- N workers consume URLs from an asyncio queue.
- A ``url_normalize`` hook rewrites children URLs to canonical form
  BEFORE they are stored / enqueued. This keeps ``_results`` keys
  consistent with the URLs that the workers actually fetch.
- When a worker parses an index sitemap, it enqueues children and
  records the parent → children relationship.
- After the queue drains, we walk the results dict bottom-up and
  rebuild index sitemaps with their actual children.

This avoids nested task groups and yields true concurrent fetching.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable

import anyio

from usp.fetch_parse import (
    IndexRobotsTxtSitemapParser,
    PlainTextSitemapParser,
    XMLSitemapParser,
)
from usp.helpers import RecurseCallbackType, RecurseListCallbackType
from usp.objects.sitemap import (
    AbstractSitemap,
    IndexRobotsTxtSitemap,
    IndexWebsiteSitemap,
    IndexXMLSitemap,
    InvalidSitemap,
)

from .async_client import AsyncWebClient, ungzipped_bytes

log = logging.getLogger(__name__)

INDEX_TYPES = (IndexXMLSitemap, IndexRobotsTxtSitemap)

URLNormalizeType = Callable[[str], str]
"""Rewrite a URL before storing / enqueueing. Useful for routing
absolute upstream URLs to a local replay server."""


def _select_parser(url: str, content: bytes, *, parser_kwargs: dict[str, Any]):
    no_fetch = lambda u, l, p: False  # noqa: E731
    stripped = content[:20].lstrip()
    if stripped.startswith(b"<"):
        return XMLSitemapParser(
            content=content.decode("utf-8", errors="replace"),
            web_client=None,
            recurse_callback=no_fetch,
            **parser_kwargs,
        )
    if url.endswith("/robots.txt"):
        return IndexRobotsTxtSitemapParser(
            content=content.decode("utf-8", errors="replace"),
            web_client=None,
            recurse_callback=no_fetch,
            **parser_kwargs,
        )
    return PlainTextSitemapParser(
        content=content.decode("utf-8", errors="replace"),
        web_client=None,
        recurse_callback=no_fetch,
        **parser_kwargs,
    )


def _extract_child_urls(parser: Any, level: int, parent_urls: set[str],
                        recurse_list_callback: RecurseListCallbackType) -> list[str]:
    raw: list[str] = []
    cp = getattr(parser, "_concrete_parser", None)
    if cp is not None and hasattr(cp, "_sub_sitemap_urls"):
        raw = list(cp._sub_sitemap_urls)
    elif isinstance(parser, IndexRobotsTxtSitemapParser):
        for line in parser._content.splitlines():
            m = re.search(r"^site-?map:\s*(.+?)$", line.strip(), flags=re.IGNORECASE)
            if m:
                raw.append(m.group(1).strip())
    return recurse_list_callback(raw, level, parent_urls)


class AsyncCrawler:
    """Flat worker-pool sitemap crawler."""

    def __init__(
        self,
        web_client: AsyncWebClient,
        *,
        concurrency: int = 8,
        max_sitemaps: int | None = None,
        max_depth: int = 16,
        deadline_s: float | None = None,
        max_uncompressed_bytes: int | None = 100 * 1024 * 1024,
        recurse_callback: RecurseCallbackType | None = None,
        recurse_list_callback: RecurseListCallbackType | None = None,
        url_normalize: URLNormalizeType | None = None,
    ):
        self._web_client = web_client
        self._concurrency = max(1, concurrency)
        self._max_sitemaps = max_sitemaps if max_sitemaps is not None else float("inf")
        self._max_depth = max_depth
        self._deadline_s = deadline_s
        self._max_uncompressed = max_uncompressed_bytes
        self._recurse_callback = recurse_callback or (lambda u, l, p: True)
        self._recurse_list_callback = recurse_list_callback or (lambda u, l, p: u)
        self._url_normalize = url_normalize or (lambda u: u)

        self._results: dict[str, AbstractSitemap] = {}
        self._children_of: dict[str, list[str]] = {}
        self._seen: set[str] = set()
        self._sitemaps_fetched = 0
        self._sitemaps_failed = 0
        self._started_at = 0.0

    @property
    def sitemaps_fetched(self) -> int:
        return self._sitemaps_fetched

    @property
    def sitemaps_failed(self) -> int:
        return self._sitemaps_failed

    def results(self) -> dict[str, AbstractSitemap]:
        return dict(self._results)

    # ---------- public API ----------

    async def crawl(self, root_urls: list[str]) -> AbstractSitemap:
        """Crawl from the given root URLs and return a synthetic root sitemap."""
        self._started_at = time.monotonic()
        norm_roots = [self._url_normalize(u) for u in root_urls]
        queue: asyncio.Queue[tuple[str, set[str], int] | None] = asyncio.Queue()
        for u in norm_roots:
            queue.put_nowait((u, set(), 0))

        async def worker() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    return
                url, parents, level = item
                try:
                    await self._process(url, parents, level, queue)
                finally:
                    queue.task_done()

        async with anyio.create_task_group() as tg:
            for _ in range(self._concurrency):
                tg.start_soon(worker)

            async def stopper():
                await queue.join()
                for _ in range(self._concurrency):
                    await queue.put(None)
            tg.start_soon(stopper)

        return self._finalize(norm_roots)

    # ---------- internals ----------

    def _is_budget_exhausted(self) -> bool:
        if self._sitemaps_fetched >= self._max_sitemaps:
            return True
        if self._deadline_s is not None and (
            time.monotonic() - self._started_at
        ) > self._deadline_s:
            return True
        return False

    async def _process(
        self,
        url: str,
        parent_urls: set[str],
        level: int,
        queue: "asyncio.Queue",
    ) -> None:
        if level > self._max_depth or self._is_budget_exhausted():
            return

        # Fetch
        try:
            response = await self._web_client.get(url)
        except Exception as ex:
            self._sitemaps_failed += 1
            self._results[url] = InvalidSitemap(url=url, reason=f"fetch: {ex}")
            return
        if response.error is not None:
            self._sitemaps_failed += 1
            self._results[url] = InvalidSitemap(url=url, reason=f"fetch: {response.error}")
            return
        status = response.status_code()
        if not (200 <= status < 300):
            self._sitemaps_failed += 1
            self._results[url] = InvalidSitemap(url=url, reason=f"HTTP {status}")
            return
        try:
            content_bytes = ungzipped_bytes(
                url, response.raw_data(), response.header("content-type"),
                max_uncompressed_bytes=self._max_uncompressed,
            )
        except Exception as ex:
            self._sitemaps_failed += 1
            self._results[url] = InvalidSitemap(url=url, reason=f"decompress: {ex}")
            return

        self._sitemaps_fetched += 1
        final_url = self._url_normalize(response.url())
        new_parents = parent_urls | {final_url}

        # Parse
        parser_kwargs = dict(
            url=final_url,
            recursion_level=level,
            parent_urls=new_parents,
            recurse_list_callback=self._recurse_list_callback,
        )
        try:
            parser = _select_parser(final_url, content_bytes, parser_kwargs=parser_kwargs)
            sitemap = parser.sitemap()
        except Exception as ex:
            log.warning("parse failed for %s: %s", final_url, ex)
            self._results[final_url] = InvalidSitemap(
                url=final_url, reason=f"parse: {ex}"
            )
            return

        # Enqueue children if index
        if isinstance(sitemap, INDEX_TYPES):
            child_urls = _extract_child_urls(
                parser, level, new_parents, self._recurse_list_callback
            )
            queued: list[str] = []
            for child in child_urls:
                norm_child = self._url_normalize(child)
                if norm_child in self._seen:
                    continue
                if not self._recurse_callback(child, level + 1, new_parents):
                    continue
                if self._is_budget_exhausted():
                    break
                self._seen.add(norm_child)
                queue.put_nowait((norm_child, new_parents, level + 1))
                queued.append(norm_child)
            self._children_of[final_url] = queued

        self._results[final_url] = sitemap

    def _finalize(self, root_urls: list[str]) -> AbstractSitemap:
        """Walk bottom-up: rebuild children before their parents."""
        self._rebuild_recursive(root_urls)
        return self._build_root(root_urls)

    def _rebuild_recursive(self, parent_urls: list[str]) -> None:
        for parent_url in parent_urls:
            child_urls = self._children_of.get(parent_url)
            if not child_urls:
                continue
            # Recurse into children first
            self._rebuild_recursive(child_urls)
            # Now children are rebuilt; build parent
            children = [self._results[c] for c in child_urls if c in self._results]
            current = self._results.get(parent_url)
            if isinstance(current, IndexXMLSitemap):
                self._results[parent_url] = IndexXMLSitemap(
                    url=parent_url, sub_sitemaps=children
                )
            elif isinstance(current, IndexRobotsTxtSitemap):
                self._results[parent_url] = IndexRobotsTxtSitemap(
                    url=parent_url, sub_sitemaps=children
                )

    def _build_root(self, root_urls: list[str]) -> AbstractSitemap:
        top: list[AbstractSitemap] = []
        for u in root_urls:
            r = self._results.get(u)
            if r is not None:
                top.append(r)
        return IndexWebsiteSitemap(
            url=root_urls[0] if root_urls else "", sub_sitemaps=top
        )


async def run_sync(coro):
    """Helper for callers that just want to drive a coroutine to completion."""
    return await coro
