"""Benchmark runner: crawl a URL via ultimate-sitemap-parser and emit metrics.

Two modes:
- replay (default): points USP at a local bench/serve_corpus.py instance.
- live:              points USP at the real network URL.

Emits a JSON record to ``bench/results/<name>.json`` per the schema in
MASTER-PLAN-usp-perf.md §2.3.
"""
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

# Make the package importable when called as a module
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

RESULTS_DIR = Path(__file__).parent / "results"

UA = "usp-bench/0.x (+https://d4t0.com/bot)"

log = logging.getLogger("bench.run")


# --- HTTP instrumentation ---------------------------------------------------

class RerouteWebClient:
    """A web client that proxies all GETs to a base URL, preserving the path/query.

    Used to redirect upstream absolute URLs (e.g. ``https://play.google.com/foo.xml``)
    to a local replay server (``http://127.0.0.1:8765/foo.xml``).

    Records per-request status, bytes_wire, bytes_uncompressed, latency in ``stat``.
    """

    __slots__ = ("__base", "stat", "__delegate")

    def __init__(self, base: str, delegate):
        self.__base = base.rstrip("/")
        self.__delegate = delegate
        self.stat: dict = {
            "requests": 0,
            "s2xx": 0, "s3xx": 0, "s4xx": 0, "s5xx": 0,
            "s0xx": 0,
            "s404": 0, "s429": 0,
            "bytes_wire": 0,
            "bytes_uncompressed": 0,
            "latencies": [],
        }

    def _rewrite(self, url: str) -> str:
        p = urlparse(url)
        new = self.__base + p.path
        if p.query:
            new += "?" + p.query
        return new

    def get(self, url: str):
        from usp.web_client.abstract_client import WebClientErrorResponse
        new_url = self._rewrite(url)
        t0 = time.monotonic()
        try:
            resp = self.__delegate.get(new_url)
        except Exception:
            elapsed = time.monotonic() - t0
            self.stat["requests"] += 1
            self.stat["s0xx"] += 1
            self.stat["latencies"].append(elapsed * 1000.0)
            raise
        elapsed = time.monotonic() - t0
        status = int(resp.status_code()) if hasattr(resp, "status_code") else 0
        data = b""
        try:
            data = resp.raw_data() or b""
        except Exception:
            pass
        self.stat["requests"] += 1
        bucket = f"s{status // 100}xx"
        if bucket in ("s2xx", "s3xx", "s4xx", "s5xx"):
            self.stat[bucket] += 1
        else:
            self.stat["s0xx"] += 1
        if status == 404:
            self.stat["s404"] += 1
        if status == 429:
            self.stat["s429"] += 1
        self.stat["bytes_wire"] += len(data)
        self.stat["bytes_uncompressed"] += len(data)
        self.stat["latencies"].append(elapsed * 1000.0)
        return resp

    def set_max_response_data_length(self, n) -> None:
        return self.__delegate.set_max_response_data_length(n)

    def set_timeout(self, t) -> None:
        return self.__delegate.set_timeout(t)

    def set_proxies(self, p) -> None:
        return self.__delegate.set_proxies(p)


def make_web_client(replay_url: str | None):
    """Build a web client. If ``replay_url`` is set, route all requests there."""
    from usp.web_client.requests_client import RequestsWebClient
    delegate = RequestsWebClient()
    if replay_url:
        return RerouteWebClient(replay_url, delegate)
    # For live: still wrap to record stats, but no rewriting
    return RerouteWebClient("", delegate) if False else _RecordingWebClient(delegate)


class _RecordingWebClient:
    """Recording-only wrapper (no URL rewriting)."""
    __slots__ = ("stat", "__delegate")

    def __init__(self, delegate):
        self.__delegate = delegate
        self.stat: dict = {
            "requests": 0,
            "s2xx": 0, "s3xx": 0, "s4xx": 0, "s5xx": 0, "s0xx": 0,
            "s404": 0, "s429": 0,
            "bytes_wire": 0,
            "bytes_uncompressed": 0,
            "latencies": [],
        }

    def get(self, url: str):
        t0 = time.monotonic()
        try:
            resp = self.__delegate.get(url)
        except Exception:
            elapsed = time.monotonic() - t0
            self.stat["requests"] += 1
            self.stat["s0xx"] += 1
            self.stat["latencies"].append(elapsed * 1000.0)
            raise
        elapsed = time.monotonic() - t0
        status = int(resp.status_code())
        try:
            data = resp.raw_data() or b""
        except Exception:
            data = b""
        self.stat["requests"] += 1
        bucket = f"s{status // 100}xx"
        if bucket in ("s2xx", "s3xx", "s4xx", "s5xx"):
            self.stat[bucket] += 1
        else:
            self.stat["s0xx"] += 1
        if status == 404:
            self.stat["s404"] += 1
        if status == 429:
            self.stat["s429"] += 1
        self.stat["bytes_wire"] += len(data)
        self.stat["bytes_uncompressed"] += len(data)
        self.stat["latencies"].append(elapsed * 1000.0)
        return resp

    def set_max_response_data_length(self, n) -> None:
        return self.__delegate.set_max_response_data_length(n)

    def set_timeout(self, t) -> None:
        return self.__delegate.set_timeout(t)

    def set_proxies(self, p) -> None:
        return self.__delegate.set_proxies(p)


# --- Crawl wrapper ----------------------------------------------------------

def crawl(homepage: str, web_client, cap: int) -> dict:
    """Run a recursive crawl with a deterministic cap."""
    from usp.tree import sitemap_tree_for_homepage

    def cap_leaves(urls: list[str], level: int, parents: set[str]) -> list[str]:
        return sorted(urls)[:cap]

    t0 = time.monotonic()
    tree = sitemap_tree_for_homepage(
        homepage,
        web_client=web_client,
        use_robots=True,
        use_known_paths=False,
        recurse_list_callback=cap_leaves,
        normalize_homepage_url=False,
    )
    wall = time.monotonic() - t0

    urls = []
    try:
        for page in tree.all_pages():
            urls.append(page.url)
    except Exception:
        pass

    sitemaps = 0
    try:
        for sm in tree.all_sitemaps():
            sitemaps += 1
    except Exception:
        pass

    return {
        "wall_s": wall,
        "urls_total": len(urls),
        "sitemaps_fetched": sitemaps,
    }


# --- Metrics aggregation ----------------------------------------------------

def summarize(stat: dict) -> dict:
    lats = sorted(stat.get("latencies", []))
    if not lats:
        return {
            "requests": stat.get("requests", 0),
            "s2xx": stat.get("s2xx", 0),
            "s3xx": stat.get("s3xx", 0),
            "s404": stat.get("s404", 0),
            "s429": stat.get("s429", 0),
            "s5xx": stat.get("s5xx", 0),
            "bytes_wire": stat.get("bytes_wire", 0),
            "bytes_uncompressed": stat.get("bytes_uncompressed", 0),
            "lat_p50_ms": 0.0,
            "lat_p95_ms": 0.0,
            "lat_p99_ms": 0.0,
        }
    def pct(p):
        i = max(0, min(len(lats) - 1, int(len(lats) * p)))
        return lats[i]
    return {
        "requests": stat.get("requests", 0),
        "s2xx": stat.get("s2xx", 0),
        "s3xx": stat.get("s3xx", 0),
        "s404": stat.get("s404", 0),
        "s429": stat.get("s429", 0),
        "s5xx": stat.get("s5xx", 0),
        "bytes_wire": stat.get("bytes_wire", 0),
        "bytes_uncompressed": stat.get("bytes_uncompressed", 0),
        "lat_p50_ms": round(pct(0.50), 1),
        "lat_p95_ms": round(pct(0.95), 1),
        "lat_p99_ms": round(pct(0.99), 1),
    }


def peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux: KB; macOS: bytes — normalize to MB
    if sys.platform == "darwin":
        return rss / 1024 / 1024
    return rss / 1024


def git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        )
        return out.strip()
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


# --- Main ---------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["replay", "live"], default="replay")
    parser.add_argument("--profile", default="zero",
                        choices=["zero", "lan", "vn-google", "hostile"])
    parser.add_argument("--replay-url", default="http://127.0.0.1:8765")
    parser.add_argument("--homepage", default=None,
                        help="Homepage URL to crawl. For replay, this is the "
                             "fake homepage discovered via robots.txt.")
    parser.add_argument("--fanout-cap", type=int, default=200)
    parser.add_argument("--name", default=None,
                        help="Output file name (default: <phase>-<profile>-<cap>).")
    parser.add_argument("--phase", default="local")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.mode == "replay":
        # Check the server is reachable
        try:
            host = args.replay_url.split("//", 1)[1].split(":", 1)[0]
            port = int(args.replay_url.rsplit(":", 1)[1].split("/", 1)[0])
            socket.create_connection((host, port), timeout=2).close()
        except Exception as e:
            log.error("replay server not reachable at %s: %r", args.replay_url, e)
            log.error("start it with: uv run bench/serve_corpus.py --profile %s", args.profile)
            return 2

    gc.collect()
    rss_before = peak_rss_mb()

    if args.mode == "replay":
        wc = make_web_client(args.replay_url)
    else:
        wc = make_web_client(None)
    homepage = args.homepage or args.replay_url

    log.info("crawl mode=%s profile=%s cap=%d homepage=%s",
             args.mode, args.profile, args.fanout_cap, homepage)
    try:
        result = crawl(homepage, wc, args.fanout_cap)
    except Exception as e:
        log.exception("crawl failed: %r", e)
        return 1

    rss_after = peak_rss_mb()
    urls_per_s = result["urls_total"] / result["wall_s"] if result["wall_s"] > 0 else 0.0

    record = {
        "run_id": str(uuid.uuid4())[:8],
        "git_sha": git_sha(),
        "phase": args.phase,
        "mode": args.mode,
        "profile": args.profile,
        "fanout_cap": args.fanout_cap,
        "homepage": homepage,
        "wall_s": round(result["wall_s"], 3),
        "urls_total": result["urls_total"],
        "sitemaps_fetched": result["sitemaps_fetched"],
        "urls_per_s": round(urls_per_s, 1),
        "http": summarize(wc.stat),
        "mem": {
            "peak_rss_mb": round(max(rss_after, rss_before), 1),
        },
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    name = args.name or f"{args.phase}-{args.profile}-cap{args.fanout_cap}"
    write_result(record, name)

    log.info("DONE wall=%.2fs urls=%d sitemaps=%d urls/s=%.1f peak_rss=%.1fMB",
             record["wall_s"], record["urls_total"], record["sitemaps_fetched"],
             record["urls_per_s"], record["mem"]["peak_rss_mb"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
