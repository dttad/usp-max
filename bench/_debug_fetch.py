"""Debug: trace fetches in detail."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import httpx


async def main():
    # Get list of sitemap URLs from the server
    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        resp = await client.get("http://127.0.0.1:8765/robots.txt")
        text = resp.text
        import re
        sitemap_urls = re.findall(r"Sitemap:\s*(\S+)", text)
        # Rewrite to local
        from urllib.parse import urlparse
        local_urls = []
        for u in sitemap_urls:
            local_urls.append("http://127.0.0.1:8765" + urlparse(u).path)
        # Get sub-sitemaps from index
        resp = await client.get("http://127.0.0.1:8765/sitemaps/sitemaps-index-0.xml")
        subs = re.findall(r"<loc>([^<]+)</loc>", resp.text)[:50]
        for u in subs:
            local_urls.append("http://127.0.0.1:8765" + urlparse(u).path)

        print(f"URLs to fetch: {len(local_urls)} (first 5: {local_urls[:5]})")
        print(f"Index fetch: {len(resp.content)} bytes, time {resp.elapsed.total_seconds()*1000:.1f}ms")

        # Time 50 parallel fetches
        async def fetch_one(url, label):
            t0 = time.monotonic()
            r = await client.get(url)
            wall = time.monotonic() - t0
            return wall, len(r.content)

        t0 = time.monotonic()
        results = await asyncio.gather(*[fetch_one(u, "") for u in local_urls])
        wall = time.monotonic() - t0

        sizes = [r[1] for r in results]
        times = sorted([r[0] for r in results])
        n = len(times)
        print(f"wall={wall:.2f}s")
        print(f"  times: min={min(times)*1000:.1f}ms p50={times[n//2]*1000:.1f}ms "
              f"p95={times[int(n*0.95)]*1000:.1f}ms max={max(times)*1000:.1f}ms")
        print(f"  bytes: total={sum(sizes)}")


if __name__ == "__main__":
    asyncio.run(main())
