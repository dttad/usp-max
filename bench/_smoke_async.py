"""Smoke test for the async crawler."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import anyio
import httpx

from usp.fetcher.async_client import AsyncWebClient
from usp.fetcher.crawler import AsyncCrawler
from usp.objects.sitemap import (
    PagesXMLSitemap, IndexXMLSitemap, IndexRobotsTxtSitemap, InvalidSitemap,
)

REPLAY_BASE = "http://127.0.0.1:8765"


def cap_leaves(urls, level, parents):
    return sorted(urls)[:50]


def normalize_to_local(url: str) -> str:
    p = urlparse(url)
    return REPLAY_BASE + p.path


async def main():
    async with AsyncWebClient(http2=True, max_connections=20) as client:
        crawler = AsyncCrawler(
            client,
            concurrency=8,
            max_sitemaps=1000,
            recurse_list_callback=cap_leaves,
            url_normalize=normalize_to_local,
        )
        t0 = time.monotonic()
        tree = await crawler.crawl([REPLAY_BASE + "/robots.txt"])
        wall = time.monotonic() - t0

    print(f"wall={wall:.2f}s fetched={crawler.sitemaps_fetched} failed={crawler.sitemaps_failed}")
    print(f"_results count: {len(crawler._results)}")
    print(f"_children_of count: {len(crawler._children_of)}")
    print(f"_seen count: {len(crawler._seen)}")

    # Type breakdown
    type_counts = {}
    for sm in crawler._results.values():
        t = type(sm).__name__
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"result types: {type_counts}")

    # Print children_of
    for parent, kids in crawler._children_of.items():
        print(f"  {parent[:60]:60s} -> {len(kids)} children")

    # Check children types for index-0
    idx0 = crawler._results.get("http://127.0.0.1:8765/sitemaps/sitemaps-index-0.xml")
    if idx0:
        print(f"idx0 type after finalize: {type(idx0).__name__}, sub_sitemaps: {len(idx0.sub_sitemaps)}")
        child_types = {}
        for s in idx0.sub_sitemaps:
            t = type(s).__name__
            child_types[t] = child_types.get(t, 0) + 1
        print(f"  idx0 children types: {child_types}")
        # Try accessing pages of one child
        if idx0.sub_sitemaps:
            child = idx0.sub_sitemaps[0]
            try:
                ps = child.pages
                print(f"  first child pages len: {len(ps)}")
            except Exception as e:
                print(f"  first child pages ERROR: {e}")
        # Same idx0 from tree
        idx0_from_tree = tree.sub_sitemaps[0].sub_sitemaps[0]
        print(f"  same id? {idx0 is idx0_from_tree}")
        print(f"  idx0_from_tree sub_sitemaps: {len(idx0_from_tree.sub_sitemaps)}")

    # Walk tree manually
    print("--- walking tree manually ---")
    def walk(node, depth=0):
        try:
            ps = list(node.pages)
        except Exception as e:
            ps = []
            print(f"{'  '*depth}ERROR pages at {node.url}: {e}")
        sub = list(node.sub_sitemaps)
        print(f"{'  '*depth}{type(node).__name__}({node.url}) pages={len(ps)} subs={len(sub)}")
        for s in sub:
            walk(s, depth + 1)

    walk(tree)

    # Build root
    root_url = REPLAY_BASE + "/robots.txt"
    root_sitemap = crawler._results.get(root_url)
    print(f"root_sitemap type: {type(root_sitemap).__name__}")
    if root_sitemap:
        print(f"root_sitemap.url: {root_sitemap.url}")
        print(f"root_sitemap.sub_sitemaps count: {len(root_sitemap.sub_sitemaps)}")

    # Check pages count via tree
    pages = list(tree.all_pages())
    print(f"tree.all_pages(): {len(pages)}")
    print(f"first page: {pages[0].url if pages else '(none)'}")


if __name__ == "__main__":
    anyio.run(main)
