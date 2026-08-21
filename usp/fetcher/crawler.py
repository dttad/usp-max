"""Async crawler with optional lxml parsing (Phase 7a).

Set ``USP_USE_LXML=1`` (default for this script) to use the lxml
backend for ``urlset`` / ``sitemapindex``. Falls back to expat when
the content is RSS/Atom.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any

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
    PagesXMLSitemap,
)
from usp.web_client.abstract_client import NoWebClientException

from .async_client import AsyncWebClient, ungzipped_bytes

log = logging.getLogger(__name__)

INDEX_TYPES = (IndexXMLSitemap, IndexRobotsTxtSitemap)

URLNormalizeType = Callable[[str], str]


def _sniff_root(content: bytes) -> str | None:
    """Return 'pages' / 'index' / 'feed' / None based on root element.

    Skips over ``<?xml ...?>`` declarations and BOM markers before
    looking at the root element.
    """
    import gzip

    raw = content
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    head = raw.lstrip()[:500].lower()
    # Skip <?xml ... ?> declaration
    while head.startswith(b"<?"):
        end = head.find(b"?>")
        if end < 0:
            break
        head = head[end + 2 :].lstrip()
    if head.startswith(b"<urlset"):
        return "pages"
    if head.startswith(b"<sitemapindex"):
        return "index"
    if head.startswith(b"<rss") or head.startswith(b"<feed"):
        return "feed"
    return None


def _select_parser_lxml(url: str, content: bytes, *, parser_kwargs: dict[str, Any]):
    """Select between lxml (pages/index) and expat (RSS/Atom/text)."""
    from .lxml_parser import (
        parse_lxml_pages,
    )

    kind = _sniff_root(content)
    no_fetch = lambda u, _lvl, _p: False  # noqa: E731

    if kind == "pages":
        pages = parse_lxml_pages(content, url)
        return PagesXMLSitemap(url=url, pages=pages)
    if kind == "index":
        # Build IndexXMLSitemap with empty children; crawler will fill them.
        return IndexXMLSitemap(url=url, sub_sitemaps=[])
    if kind == "feed":
        # Fallback to expat for RSS/Atom for now.
        from usp.fetch_parse import PagesAtomSitemapParser, PagesRSSSitemapParser

        text = content.decode("utf-8", errors="replace")
        stripped = content.lstrip()[:50].lower()
        if stripped.startswith(b"<feed"):
            return PagesAtomSitemapParser(
                content=text, recurse_callback=no_fetch, **parser_kwargs
            ).sitemap()
        return PagesRSSSitemapParser(
            content=text, recurse_callback=no_fetch, **parser_kwargs
        ).sitemap()
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


def _select_parser_expat(url: str, content: bytes, *, parser_kwargs: dict[str, Any]):
    """Original expat-based parser."""
    no_fetch = lambda u, _lvl, _p: False  # noqa: E731
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


def _extract_child_urls_from_lxml_index(
    parser, content: bytes, level: int, parent_urls: set[str], recurse_list_callback
) -> list[str]:
    """After lxml index parsing, we need to re-parse for the child URLs."""
    from .lxml_parser import parse_lxml_index

    raw = content
    return recurse_list_callback(parse_lxml_index(raw, ""), level, parent_urls)


class AsyncCrawler:
    """Flat worker-pool sitemap crawler with optional lxml backend."""

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
        use_lxml: bool | None = None,
    ):
        self._web_client = web_client
        self._concurrency = max(1, concurrency)
        self._max_sitemaps = max_sitemaps if max_sitemaps is not None else float("inf")
        self._max_depth = max_depth
        self._deadline_s = deadline_s
        self._max_uncompressed = max_uncompressed_bytes
        self._recurse_callback = recurse_callback or (lambda u, _lvl, _p: True)
        self._recurse_list_callback = recurse_list_callback or (lambda u, _lvl, _p: u)
        self._url_normalize = url_normalize or (lambda u: u)
        if use_lxml is None:
            use_lxml = bool(int(os.environ.get("USP_USE_LXML", "1")))
        self._use_lxml = use_lxml

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
        if (
            self._deadline_s is not None
            and (time.monotonic() - self._started_at) > self._deadline_s
        ):
            return True
        return False

    async def _process(
        self,
        url: str,
        parent_urls: set[str],
        level: int,
        queue: asyncio.Queue,
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
            self._results[url] = InvalidSitemap(
                url=url, reason=f"fetch: {response.error}"
            )
            return
        status = response.status_code()
        if not (200 <= status < 300):
            self._sitemaps_failed += 1
            self._results[url] = InvalidSitemap(url=url, reason=f"HTTP {status}")
            return
        try:
            content_bytes = ungzipped_bytes(
                url,
                response.raw_data(),
                response.header("content-type"),
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
        kind = _sniff_root(content_bytes)
        try:
            if self._use_lxml and kind in ("pages", "index"):
                from .lxml_parser import parse_lxml_index, parse_lxml_pages

                if kind == "pages":
                    pages = parse_lxml_pages(content_bytes, final_url)
                    sitemap = PagesXMLSitemap(url=final_url, pages=pages)
                else:  # index
                    child_urls = parse_lxml_index(content_bytes, final_url)
                    sitemap = IndexXMLSitemap(url=final_url, sub_sitemaps=[])
            else:
                # Expat fallback
                parser = _select_parser_expat(
                    final_url, content_bytes, parser_kwargs=parser_kwargs
                )
                sitemap = parser.sitemap()
        except NoWebClientException:
            self._results[url] = InvalidSitemap(url=url, reason="un-fetched child")
            return
        except Exception as ex:
            log.warning("parse failed for %s: %s", final_url, ex)
            self._results[final_url] = InvalidSitemap(
                url=final_url, reason=f"parse: {ex}"
            )
            return

        # Enqueue children if index
        if isinstance(sitemap, INDEX_TYPES):
            if self._use_lxml and kind == "index":
                # We already have child_urls from the lxml parse
                child_urls_raw = child_urls
            else:
                # Re-extract via expat
                # We need the parser — but expat parsers store URLs internally
                from usp.fetch_parse import IndexRobotsTxtSitemapParser

                parser = _select_parser_expat(
                    final_url, content_bytes, parser_kwargs=parser_kwargs
                )
                if (
                    hasattr(parser, "_concrete_parser")
                    and parser._concrete_parser is not None
                ):
                    cp = parser._concrete_parser
                    child_urls_raw = list(getattr(cp, "_sub_sitemap_urls", []))
                elif isinstance(parser, IndexRobotsTxtSitemapParser):
                    child_urls_raw = []
                    for line in parser._content.splitlines():
                        m = re.search(
                            r"^site-?map:\s*(.+?)$", line.strip(), flags=re.IGNORECASE
                        )
                        if m:
                            child_urls_raw.append(m.group(1).strip())
                else:
                    child_urls_raw = []
            child_urls = self._recurse_list_callback(child_urls_raw, level, new_parents)
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

    def _rebuild_recursive(self, parent_urls: list[str]) -> None:
        for parent_url in parent_urls:
            child_urls = self._children_of.get(parent_url)
            if not child_urls:
                continue
            self._rebuild_recursive(child_urls)
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

    def _finalize(self, root_urls: list[str]) -> AbstractSitemap:
        self._rebuild_recursive(root_urls)
        return self._build_root(root_urls)

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
    return await coro
