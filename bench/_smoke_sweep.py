"""Quick concurrency sweep for the async crawler (vn-google profile)."""
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
    p = urlparse(url)
    return REPLAY_BASE + p.path


async def run_one(conc: int, cap: int):
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
        f"conc={conc:4d} cap={cap:3d} wall={wall:6.2f}s "
        f"fetched={crawler.sitemaps_fetched} failed={crawler.sitemaps_failed} "
        f"urls={len(urls)} urls/s={rate:7.1f}",
        flush=True,
    )


async def main():
    print("=== cap=50 (100 subs expected, ~50 succeed in corpus) ===", flush=True)
    for conc in (1, 8, 32):
        await run_one(conc, 50)
    print("=== cap=200 (400 subs expected, ~250 succeed in corpus) ===", flush=True)
    for conc in (8, 16, 32, 64):
        await run_one(conc, 200)


if __name__ == "__main__":
    anyio.run(main)
