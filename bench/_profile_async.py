"""Profile the async crawler by measuring per-stage times."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import httpx

from usp.fetcher.async_client import AsyncWebClient, ungzipped_bytes
from usp.fetcher.crawler import _select_parser, _extract_child_urls, AsyncCrawler
from usp.objects.sitemap import (
    InvalidSitemap, IndexXMLSitemap, IndexRobotsTxtSitemap,
)


def cap_n(n: int):
    def _cap(urls, level, parents):
        return sorted(urls)[:n]
    return _cap


async def main():
    async with AsyncWebClient(http2=True, max_connections=50) as client:
        crawler = AsyncCrawler(
            client,
            concurrency=16,
            max_sitemaps=10000,
            recurse_list_callback=cap_n(50),
        )
        await crawler._ensure_primitives()
        crawler._started_at = time.monotonic()

        timings = {"fetch": [], "decompress": [], "parse": [], "build": []}
        counter = {"n": 0}

        async def timed_one(url, parents, level):
            if level > crawler._max_depth or crawler._is_budget_exhausted():
                return
            async with crawler._fetch_sem:
                tr = time.monotonic()
                response = await client.get(url)
                tf = time.monotonic() - tr
                if response.error is not None or not (200 <= response.status_code() < 300):
                    return
                tr = time.monotonic()
                try:
                    content_bytes = ungzipped_bytes(
                        url, response.raw_data(), response.header("content-type")
                    )
                except Exception:
                    return
                td = time.monotonic() - tr

            counter["n"] += 1

            tr = time.monotonic()
            kwargs = dict(url=response.url(), recursion_level=level,
                          parent_urls=parents | {response.url()},
                          recurse_list_callback=crawler._recurse_list_callback)
            parser = _select_parser(response.url(), content_bytes, parser_kwargs=kwargs)
            sitemap = parser.sitemap()
            tp = time.monotonic() - tr

            timings["fetch"].append(tf)
            timings["decompress"].append(td)
            timings["parse"].append(tp)

            if isinstance(sitemap, (IndexXMLSitemap, IndexRobotsTxtSitemap)):
                new_parents = parents | {response.url()}
                child_urls = _extract_child_urls(
                    parser, level, new_parents, crawler._recurse_list_callback
                )
                new_seen = set()
                for child in child_urls:
                    if child in crawler._seen:
                        continue
                    if not crawler._recurse_callback(child, level + 1, new_parents):
                        continue
                    crawler._seen.add(child)
                    new_seen.add(child)

                if new_seen:
                    async def _child(child_url):
                        await timed_one(child_url, new_parents, level + 1)
                    async with crawler._fetch_sem.__class__(1):
                        pass  # placeholder
                    import anyio
                    async with anyio.create_task_group() as tg:
                        for c in new_seen:
                            tg.start_soon(_child, c)
                    tr = time.monotonic()
                    children = []
                    for child in new_seen:
                        res = crawler._results.get(child)
                        if res is not None:
                            children.append(res)
                    if isinstance(sitemap, IndexXMLSitemap):
                        sitemap = IndexXMLSitemap(url=response.url(), sub_sitemaps=children)
                    elif isinstance(sitemap, IndexRobotsTxtSitemap):
                        sitemap = IndexRobotsTxtSitemap(url=response.url(), sub_sitemaps=children)
                    tb = time.monotonic() - tr
                    timings["build"].append(tb)

            await crawler._store(response.url(), sitemap)

        t0 = time.monotonic()
        async with crawler._fetch_sem.__class__(1):
            pass
        import anyio
        async with anyio.create_task_group() as tg:
            tg.start_soon(timed_one, "http://127.0.0.1:8765/robots.txt", set(), 0)
        wall = time.monotonic() - t0

    def stats(name, vals):
        if not vals:
            print(f"  {name:12s} count=0")
            return
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        print(f"  {name:12s} count={n:4d} min={min(vals)*1000:6.1f}ms "
              f"p50={vals_sorted[n//2]*1000:6.1f}ms "
              f"max={max(vals)*1000:6.1f}ms "
              f"sum={sum(vals):6.2f}s", flush=True)

    print(f"wall={wall:.2f}s crawled={counter['n']}", flush=True)
    stats("fetch", timings["fetch"])
    stats("decompress", timings["decompress"])
    stats("parse", timings["parse"])
    stats("build", timings["build"])


if __name__ == "__main__":
    asyncio.run(main())
