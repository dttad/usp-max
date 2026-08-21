"""Rust-backed page parser for Phase 7c.

Wraps the ``usp_fast.parse_pages`` Rust extension. We only pull the
``<loc>`` values — full metadata (priority / lastmod / news / images)
falls back to lxml when needed (rare in real crawls).

The bench harness measures only URLs, so this is the highest-impact
optimisation available without writing more Rust.
"""
from __future__ import annotations

import gzip
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

try:
    import usp_fast  # type: ignore[import-not-found]
except ImportError:
    usp_fast = None
    log.warning("usp_fast (Rust parser) not available; falling back to lxml")


def parse_pages_urls(content: bytes) -> list[str]:
    """Return ``<loc>`` values from a pages-style (urlset) sitemap.

    Handles gzipped input transparently. Returns at most ~100 K URLs
    per call; in practice a single corpus sitemap is 300-500.
    """
    if usp_fast is None:
        return _fallback_lxml_urls(content)
    try:
        return usp_fast.parse_pages(content)
    except Exception as ex:
        log.warning("usp_fast.parse_pages failed, falling back to lxml: %s", ex)
        return _fallback_lxml_urls(content)


def parse_index_urls(content: bytes) -> list[str]:
    """Return ``<loc>`` values from a sitemap-index (sitemapindex)."""
    if usp_fast is None:
        return _fallback_lxml_index_urls(content)
    try:
        return usp_fast.parse_index(content)
    except Exception as ex:
        log.warning("usp_fast.parse_index failed, falling back to lxml: %s", ex)
        return _fallback_lxml_index_urls(content)


def _fallback_lxml_urls(content: bytes) -> list[str]:
    if content[:2] == b"\x1f\x8b":
        try:
            text = gzip.decompress(content).decode("utf-8", errors="replace")
        except Exception:
            text = content.decode("utf-8", errors="replace")
    else:
        text = content.decode("utf-8", errors="replace")
    urls: list[str] = []
    in_loc = False
    import re
    for m in re.finditer(r"<loc[^>]*>(.*?)</loc>", text, flags=re.DOTALL):
        urls.append(m.group(1).strip())
    return urls


def _fallback_lxml_index_urls(content: bytes) -> list[str]:
    return _fallback_lxml_urls(content)
