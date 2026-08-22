"""Resolve a homepage URL to the list of sitemap URLs to crawl.

The streaming crawler (``usp.fetcher.crawler.AsyncCrawler.crawl_streaming``)
takes a list of *sitemap URLs* as input and fetches + parses each one.
The upstream sync API (``usp.tree.sitemap_tree_for_homepage``) also
takes a *homepage* URL but first reads ``/robots.txt`` (Sitemap: lines)
and probes a few known paths to discover the real sitemap locations.

Without discovery, ``crawl https://play.google.com/`` fetches the
homepage, follows redirects, and tries to parse the final HTML as
XML — which obviously fails. With discovery, ``robots.txt`` is read
and the two real sitemap URLs (``/sitemaps/sitemaps-index-0.xml`` and
``/sitemaps/sitemaps-index-1.xml``) are fed to the streaming crawler.

The heuristic used by ``looks_like_sitemap_url`` lets the caller
short-circuit discovery when the supplied URL is already a sitemap
file (e.g. ``https://example.com/sitemap.xml``).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlparse


def looks_like_sitemap_url(url: str) -> bool:
    """Return True if ``url`` clearly points at a sitemap document.

    Used to skip the discovery step when the caller already supplied a
    sitemap URL. Heuristic: any URL ending in ``.xml`` (case-insensitive)
    or whose path component contains ``/sitemap``.
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(".xml") or "/sitemap" in path


KNOWN_PATHS: tuple[str, ...] = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/sitemap.xml",
    "/robots.txt",  # last-resort probe handled separately
)


_XML_HEAD = re.compile(rb"^\s*(<\?xml|<urlset|<sitemapindex|<rss|<feed)", re.I)


def _looks_like_xml(data: bytes) -> bool:
    return bool(_XML_HEAD.match(data[:500]))


async def discover_sitemap_urls(
    homepage: str,
    fetch: Callable[[str], Awaitable[tuple[int, bytes, str]]],
    *,
    max_known_paths: int = 5,
) -> list[str]:
    """Resolve ``homepage`` to a list of sitemap URLs to crawl.

    Parameters
    ----------
    homepage
        A homepage URL such as ``https://example.com/``.
    fetch
        An async callable taking a URL and returning
        ``(status_code, body_bytes, final_url)``. ``status_code`` of 0
        means "transport error". The function does **not** raise; it
        swallows network errors so a single failed probe does not abort
        the whole discovery.
    max_known_paths
        Cap on how many ``/sitemap*.xml`` paths to probe.

    Returns
    -------
    list[str]
        Discovered sitemap URLs in priority order (robots.txt first,
        then known paths). Empty list means nothing was found; the
        caller should fall back to the supplied URL itself.
    """
    parsed = urlparse(homepage)
    if not parsed.scheme or not parsed.netloc:
        return []
    base = f"{parsed.scheme}://{parsed.netloc}"

    found: list[str] = []

    # 1) robots.txt → Sitemap: lines
    robots_url = urljoin(base, "/robots.txt")
    try:
        status, body, _ = await fetch(robots_url)
        if 200 <= status < 300 and body:
            for line in body.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    url = line.split(":", 1)[1].strip()
                    if url and url not in found:
                        found.append(url)
    except Exception:  # noqa: BLE001
        pass

    if found:
        return found

    # 2) Probe known paths (parallel, but capped so we don't hammer).
    candidates = [urljoin(base, p) for p in KNOWN_PATHS if p != "/robots.txt"][
        :max_known_paths
    ]

    async def _probe(url: str) -> str | None:
        try:
            status, body, final = await fetch(url)
        except Exception:  # noqa: BLE001
            return None
        if 200 <= status < 300 and _looks_like_xml(body):
            return final
        return None

    results = await asyncio.gather(*[_probe(u) for u in candidates])
    for r in results:
        if r and r not in found:
            found.append(r)
    return found


__all__ = ["looks_like_sitemap_url", "discover_sitemap_urls"]
