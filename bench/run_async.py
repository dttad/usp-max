"""Async-mode benchmark runner: wraps bench/run.py but uses AsyncCrawler."""
from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import resource
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

RESULTS_DIR = Path(__file__).parent / "results"

UA = "usp-bench-async/0.x (+https://d4t0.com/bot)"

log = logging.getLogger("bench.run_async")


def peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / 1024 / 1024
    return rss / 1024


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def write_result(record: dict, name: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(record, f, indent=2)
    tmp.replace(path)
    log.info("wrote %s", path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="zero",
                        choices=["zero", "lan", "vn-google", "hostile"])
    parser.add_argument("--replay-url", default="http://127.0.0.1:8765")
    parser.add_argument("--fanout-cap", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--name", default=None)
    parser.add_argument("--phase", default="P3-async")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    try:
        host = args.replay_url.split("//", 1)[1].split(":", 1)[0]
        port = int(args.replay_url.rsplit(":", 1)[1].split("/", 1)[0])
        socket.create_connection((host, port), timeout=2).close()
    except Exception as e:
        log.error("replay server not reachable: %r", e)
        return 2

    from urllib.parse import urlparse
    import anyio

    from usp.fetcher.async_client import AsyncWebClient
    from usp.fetcher.crawler import AsyncCrawler

    def cap_leaves(urls, level, parents):
        return sorted(urls)[:args.fanout_cap]

    base = args.replay_url.rstrip("/")

    def normalize_to_local(url: str) -> str:
        return base + urlparse(url).path

    async def run_async():
        nonlocal_urls = []
        stats = {
            "requests": 0, "s2xx": 0, "s3xx": 0, "s4xx": 0, "s5xx": 0,
            "s404": 0, "s429": 0,
            "bytes_wire": 0, "bytes_uncompressed": 0,
            "latencies": [],
        }

        class _RecordingClient(AsyncWebClient):
            async def get(self, url):
                t0 = time.monotonic()
                resp = await super().get(url)
                elapsed = time.monotonic() - t0
                status = resp.status_code()
                data = resp.raw_data() or b""
                stats["requests"] += 1
                bucket = f"s{status // 100}xx"
                if bucket in ("s2xx", "s3xx", "s4xx", "s5xx"):
                    stats[bucket] += 1
                if status == 404:
                    stats["s404"] += 1
                if status == 429:
                    stats["s429"] += 1
                stats["bytes_wire"] += len(data)
                stats["bytes_uncompressed"] += len(data)
                stats["latencies"].append(elapsed * 1000.0)
                return resp

        async with _RecordingClient(http2=True, max_connections=max(args.concurrency * 2, 50)) as client:
            crawler = AsyncCrawler(
                client,
                concurrency=args.concurrency,
                max_sitemaps=20000,
                recurse_list_callback=cap_leaves,
                url_normalize=normalize_to_local,
            )
            t0 = time.monotonic()
            tree = await crawler.crawl([base + "/robots.txt"])
            wall = time.monotonic() - t0

            urls = list(tree.all_pages())
            return {
                "wall_s": wall,
                "urls_total": len(urls),
                "sitemaps_fetched": crawler.sitemaps_fetched,
                "stat": stats,
            }, tree

    gc.collect()
    rss_before = peak_rss_mb()
    result, tree = anyio.run(run_async)
    rss_after = peak_rss_mb()

    wall = result["wall_s"]
    urls = result["urls_total"]
    fetched = result["sitemaps_fetched"]
    stat = result["stat"]

    urls_per_s = urls / wall if wall > 0 else 0.0

    def pct(p):
        lats = sorted(stat.get("latencies", []))
        if not lats:
            return 0.0
        i = max(0, min(len(lats) - 1, int(len(lats) * p)))
        return lats[i]

    http_summary = {
        "requests": stat["requests"],
        "s2xx": stat["s2xx"],
        "s3xx": stat["s3xx"],
        "s404": stat["s404"],
        "s429": stat["s429"],
        "s5xx": stat["s5xx"],
        "bytes_wire": stat["bytes_wire"],
        "bytes_uncompressed": stat["bytes_uncompressed"],
        "lat_p50_ms": round(pct(0.50), 1),
        "lat_p95_ms": round(pct(0.95), 1),
        "lat_p99_ms": round(pct(0.99), 1),
    }

    record = {
        "run_id": str(uuid.uuid4())[:8],
        "git_sha": git_sha(),
        "phase": args.phase,
        "mode": "replay-async",
        "profile": args.profile,
        "fanout_cap": args.fanout_cap,
        "concurrency": args.concurrency,
        "homepage": args.replay_url,
        "wall_s": round(wall, 3),
        "urls_total": urls,
        "sitemaps_fetched": fetched,
        "urls_per_s": round(urls_per_s, 1),
        "http": http_summary,
        "mem": {
            "peak_rss_mb": round(max(rss_after, rss_before), 1),
        },
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    name = args.name or f"{args.phase}-{args.profile}-cap{args.fanout_cap}"
    write_result(record, name)

    log.info("DONE wall=%.2fs urls=%d sitemaps=%d urls/s=%.1f peak_rss=%.1fMB",
             wall, urls, fetched, urls_per_s, record["mem"]["peak_rss_mb"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
