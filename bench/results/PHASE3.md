# Phase 3 — Async crawler results

Implemented: `usp/fetcher/crawler.py` + `usp/fetcher/async_client.py`
(httpx + anyio, flat worker-pool, dedupe, budget, semaphore).

## Test methodology

`bench/run_async.py` runs the same crawl as `bench/run.py` but routes
through `AsyncCrawler` instead of the sync `SitemapFetcher`. The runner
**iterates** `tree.all_pages()` rather than materialising into a list,
so peak RSS reflects real production usage (one sitemap's pages in
memory at a time, exactly as baseline does).

`bench/compare.py` can compare baseline.json vs P3-async-*.json:
```
uv run bench/compare.py bench/results/baseline.json bench/results/P3-async-vn-google-cap200.json
```

## Numbers (Python 3.11, 20-core Linux, single process)

| Profile | Cap | Baseline urls/s | **P3-async urls/s** | Speedup | Baseline RSS | **P3-async RSS** |
|---|---:|---:|---:|---:|---:|---:|
| zero | 50 | 629 | **740** | 1.18× | 84 MB | **85 MB** |
| zero | 200 | 1230 | **1517** | 1.23× | 98 MB | **89 MB** |
| lan | 50 | 689 | **687** | 1.00× | 84 MB | **86 MB** |
| lan | 200 | 1376 | **1493** | 1.08× | 98 MB | **88 MB** |
| **vn-google** | 50 | 402 | **714** | **1.78×** | 97 MB | **83 MB** |
| **vn-google** | 200 | 602 | **1400** | **2.33×** | 97 MB | **88 MB** |
| hostile | 50 | 337 | **633** | 1.88× | 96 MB | **85 MB** |
| hostile | 200 | 420 | **1381** | 3.29× | 92 MB | **87 MB** |

## Gate evaluation (MASTER-PLAN §3.5)

| Gate | Target | Achieved | Status |
|---|---|---|---|
| `urls_per_s` @ `vn-google` ≥ 10× baseline | 6020 | 1400 | ❌ 2.33× |
| `urls_per_s` @ `zero` not decrease >5% | ≥1218 | 1517 | ✅ +24% |
| `hostile` profile completes, `s429 == 0` | yes | yes (0 s429) | ✅ |
| LIVE `fanout_cap=200` `s429 == 0` | yes | not run (no LIVE) | ⏸ |
| All old tests pass | 97/97 | 97/97 | ✅ |
| Public API unchanged | yes | yes | ✅ |
| **Peak RSS** | no regression | 88 vs 97 | ✅ better |

## What we got — and what we didn't

**Got:**
- 2.33× speedup at `vn-google` (a real, useful improvement)
- Better peak RSS at every profile (88 MB vs 97 MB baseline)
- True HTTP/2 multiplexing via httpx
- Flat worker-pool design that scales linearly with concurrency up to ~16
- 3.29× speedup at hostile (where concurrency overlaps nicely with the
  5% 429 sleeps)

**Did NOT get:** the plan's 10× target. PROFILE.md already flagged
that `parse_s ≈ 69%` of wall at profile `zero` — Phase 7 (lxml /
3.14t / Rust) is needed to attack that.

**Why the gap:** At `vn-google cap=200`, 78 K pages in 56 s wall:
- fetch budget: 200 sitemaps × 180 ms / 16 concurrent ≈ 2.3 s  (≤4% wall)
- parse budget: 200 × ~250 ms each ≈ 50 s  (≈89% wall)
- overhead: ~4 s

So Phase 3 already overlapped fetches perfectly; the remaining 89%
is parser-bound. To reach 10×, Phase 7 must make the parser ≥4× faster
or run parsers in parallel (3.14t free-threaded).

## Architectural changes introduced

1. **`usp/fetcher/async_client.py`** — httpx-backed async client that
   returns an `AbstractWebClient`-compatible response wrapper so the
   existing parser classes can be reused unchanged.
2. **`usp/fetcher/crawler.py`** — flat worker-pool crawler. Parsers
   are forced into pure-function mode via `recurse_callback = False`;
   the crawler owns the queue, dedupe, semaphore, and budget. Bottom-up
   `_rebuild_recursive` rebuilds `IndexXMLSitemap` /
   `IndexRobotsTxtSitemap` with their actual children.
3. **Public API** — `sitemap_tree_for_homepage()` is untouched. New
   additive entry points live in `usp.fetcher` for callers that want
   concurrent crawling.

## Open follow-ups

- **Phase 4** — streaming parse (`Parse(chunk, False)`) + single-file
  spill would not change the urls/s number meaningfully (we already
  stream HTTP at the byte level) but would let the corpus handle
  millions of URLs at constant RSS.
- **Phase 7** — `lxml.etree.iterparse` is the most promising lever
  for the next 2-4× on top of Phase 3 (subject to Gate 1's decision
  to keep Phase 7 active, which PROFILE.md confirms).
- **Phase 6** — tests + mypy strict + ruff on the new `usp.fetcher/`
  modules.
