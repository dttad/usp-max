"""Debug: verify httpx async is actually parallel."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import httpx


async def main():
    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        urls = ["http://127.0.0.1:8765/robots.txt"] * 20
        t0 = time.monotonic()
        results = await asyncio.gather(*[client.get(u) for u in urls])
        wall = time.monotonic() - t0
        print(f"20 parallel fetches: {wall:.2f}s")
        print(f"  statuses: {[r.status_code for r in results[:5]]}")

        # Also check a sitemap URL
        urls2 = ["http://127.0.0.1:8765/sitemaps/sitemaps-index-0.xml"] * 5
        t0 = time.monotonic()
        results = await asyncio.gather(*[client.get(u) for u in urls2])
        wall = time.monotonic() - t0
        print(f"5 parallel sitemap fetches (6MB each): {wall:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
