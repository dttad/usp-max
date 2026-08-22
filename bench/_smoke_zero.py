"""Quick concurrency sweep on zero profile."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import anyio

from usp.fetcher.async_client import AsyncWebClient
from usp.fetcher.crawler import AsyncCrawler

REPLAY_BASE = "http://127.0.0.1:8765"


def cap_n(n: int):
    def _cap(urls, level, parents):
        return sorted(urls)[:n]
    return _cap


def normalize_to_local(url: str) -> str:
    return REPLAY_BASE + urlparse(url).path


async def run_one(conc: int, cap: int, label: str):
    async with AsyncWebClient(http2=True, max_connections=max(conc * 2, 50)) as client:
        crawler = AsyncCrawler(
            client,
            concurrency=conc,
            max_sitemaps=20000,
            recurse_list_callback=cap_n(cap),
            url_normalize=normalize_to_local,
        )
        t0 = time.monotonic()
        tree = await crawler.crawl([REPLAY_BASE + "/robots.txt"])
        wall = time.monotonic() - t0
    urls = list(tree.all_pages())
    rate = len(urls) / wall if wall > 0 else 0.0
    print(
        f"profile={label:9s} conc={conc:3d} cap={cap:3d} wall={wall:6.2f}s "
        f"fetched={crawler.sitemaps_fetched} failed={crawler.sitemaps_failed} "
        f"urls={len(urls)} urls/s={rate:7.1f}",
        flush=True,
    )


async def main():
    print("=== zero cap=200 (parse-bound) ===", flush=True)
    for c in (1, 4, 16, 64):
        await run_one(c, 200, "zero")


if __name__ == "__main__":
    anyio.run(main)
