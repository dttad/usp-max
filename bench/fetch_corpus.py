"""One-shot corpus fetcher for play.google.com sitemaps.

Downloads the index files and a deterministic subset of sub-sitemap files
from play.google.com. Saves raw bytes (preserving gzip) plus headers
into ``bench/corpus/`` with a ``manifest.json`` mapping URL -> path.

Selection policy: take the FIRST ``--sub-count`` URLs from the index (sorted
lexicographically). This guarantees the corpus matches what
``recurse_list_callback=cap_leaves`` will pick during benchmarking.

Politeness rules (see MASTER-PLAN-usp-perf.md §1):
- Concurrency capped at 6.
- Real UA with contact URL.
- Honors Retry-After.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = "usp-bench/0.x (+https://d4t0.com/bot)"
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

CORPUS_DIR = Path(__file__).parent / "corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"

INDEX_URLS = [
    "https://play.google.com/sitemaps/sitemaps-index-0.xml",
    "https://play.google.com/sitemaps/sitemaps-index-1.xml",
]

log = logging.getLogger("bench.fetch_corpus")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip"})
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(
        max_retries=retry, pool_connections=8, pool_maxsize=8
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def fetch_to_bytes(session: requests.Session, url: str) -> bytes:
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.content


def safe_name(url: str) -> str:
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    return f"{h}.bin"


def fetch_one_sub(session: requests.Session, url: str, dest: Path) -> dict:
    data = fetch_to_bytes(session, url)
    sha = hashlib.sha256(data).hexdigest()
    dest.write_bytes(data)
    return {
        "url": url,
        "path": str(dest.relative_to(CORPUS_DIR)),
        "sha256": sha,
        "bytes": len(data),
        "is_gzip": bool(data[:2] == b"\x1f\x8b"),
    }


def parse_index(xml_bytes: bytes) -> list[str]:
    root = ET.fromstring(xml_bytes)
    return [loc.text.strip() for loc in root.findall("sm:sitemap/sm:loc", NS) if loc.text]


def write_manifest(path: Path, entries: list[dict]) -> None:
    tmp = path.with_suffix(".json.tmp")
    payload = {
        "version": 1,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entries": entries,
    }
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sub-count", type=int, default=250,
                        help="Number of sub-sitemaps to fetch.")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--strategy", choices=["first", "spread"], default="first",
                        help="first = sort, take N; spread = evenly sample across index.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()

    log.info("Fetching %d index files...", len(INDEX_URLS))
    index_entries: list[dict] = []
    for iurl in INDEX_URLS:
        data = fetch_to_bytes(session, iurl)
        sha = hashlib.sha256(data).hexdigest()
        idx_path = CORPUS_DIR / ("index-" + iurl.rsplit("-", 1)[1])
        idx_path.write_bytes(data)
        index_entries.append({
            "url": iurl,
            "path": idx_path.name,
            "sha256": sha,
            "bytes": len(data),
            "role": "index",
        })
        log.info("  index %s -> %d bytes", iurl, len(data))

    all_subs: list[str] = []
    for entry in index_entries:
        data = (CORPUS_DIR / entry["path"]).read_bytes()
        subs = parse_index(data)
        log.info("  parsed %d sub-sitemaps from %s", len(subs), entry["path"])
        all_subs.extend(subs)

    if args.strategy == "first":
        chosen = sorted(all_subs)[: args.sub_count]
    else:
        n = len(all_subs)
        step = max(1, n // args.sub_count)
        chosen = sorted(all_subs)[::step][: args.sub_count]
    log.info("Selected %d sub-sitemaps (strategy=%s)", len(chosen), args.strategy)

    sub_entries: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {
            ex.submit(fetch_one_sub, session, url, CORPUS_DIR / safe_name(url)): url
            for url in chosen
        }
        done = 0
        for fut in as_completed(futures):
            try:
                entry = fut.result()
            except Exception as e:
                log.error("FAIL %s: %r", futures[fut], e)
                continue
            sub_entries.append(entry)
            done += 1
            if done % 25 == 0 or done == len(chosen):
                log.info("  fetched %d/%d sub-sitemaps", done, len(chosen))

    entries = sorted(index_entries + sub_entries, key=lambda e: e["url"])
    write_manifest(MANIFEST_PATH, entries)
    log.info("Wrote manifest with %d entries to %s", len(entries), MANIFEST_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
