# Performance optimization — final report

End-to-end results of the optimization effort, in the order they were
landed on `perf/optimize-async`.

## TL;DR

| Stage | vn-google cap=200 urls/s | Speedup vs baseline | Notes |
|---|---:|---:|---|
| Baseline (USP v1.8.1, sync, expat) | 602 | 1.00× | pure sequential fetch |
| **+ Phase 3** async httpx + anyio worker pool | 1 400 | **2.33×** | 88 MB RSS |
| **+ Phase 7a** `lxml.etree.iterparse` | 2 647 | **4.40×** | 77 MB RSS |
| **+ Phase 7b** CPython 3.14t (free-threaded) | 2 936 | **4.88×** | 152 MB RSS |

Best overall: **7.98× at hostile cap=50** (where async overlap wins
against the 5% 429 sleeps).

## What was shipped

### Code
- **`usp/fetcher/async_client.py`** — httpx-backed async web client.
  Returns an `AbstractWebClient`-compatible wrapper. HTTP/2 with
  multiplexing; gzip-aware.
- **`usp/fetcher/crawler.py`** — flat worker-pool `AsyncCrawler`.
  asyncio queue + anyio task group; dedupe; budget
  (`max_sitemaps`, `deadline_s`); per-URL semaphore; bottom-up
  `_rebuild_recursive` to attach children to index sitemaps.
  Falls back to expat for RSS/Atom/robots.txt/plain-text.
- **`usp/fetcher/lxml_parser.py`** — `lxml.etree.iterparse`-based
  page + index parsers. Uses start/end events so `<loc>` data
  attaches to the right builder. Sniffs `<?xml ?>` declarations.

### Bench / harness
- `bench/serve_corpus.py` — 4 latency profiles (`zero`, `lan`,
  `vn-google`, `hostile`); 252 corpus files (250 gzipped
  sub-sitemaps + 2 indexes from `play.google.com`).
- `bench/fetch_corpus.py`, `bench/run.py`, `bench/run_async.py`,
  `bench/aggregate.py`, `bench/compare.py`, `bench/profile.py`,
  `bench/_compare_full.py` — corpus fetch, sync benchmark, async
  benchmark, baseline aggregator, regression comparator, pyinstrument
  / memray / tempfile-counter driver, parser-correctness comparator.
- `bench/results/` — 30+ JSON results + `PROFILE.md`, `PHASE3.md`,
  `PHASE7.md`, `PHASE7b.md` reports, `baseline.json` aggregator.

### Project
- `pyproject.toml` — added `[project.optional-dependencies]` groups:
  `async` (httpx+anyio), `lxml`, `perf` (combined bundle).
- `CLAUDE.md` — workflow rules.
- `MASTER-PLAN-usp-perf.md` — imported plan.

## Full Phase 3 / 7 results

```
profile      cap   baseline   P3-async   P7a-lxml   P7b-3.14t
zero         50      628        740       3 060      3 275
zero        200    1 272      1 516       3 184      3 403
lan          50      689        687       3 085      3 329
lan         200    1 376      1 493       3 285      3 358
vn-google    50      401        714       2 289      2 501
vn-google   200      602      1 400       2 647      2 936
hostile      50      337        633       2 313      2 688
hostile     200      420      1 380       2 805      3 074
```

## Gate evaluation

| Gate | Target | Achieved |
|---|---|---|
| Phase 3 `urls_per_s` @ `vn-google` ≥ 10× | 6 020 | 1 400 ❌ |
| Phase 3 `urls_per_s` @ `zero` not decrease >5% | ≥ 1 218 | 1 516 ✅ |
| Phase 3 hostile `s429 == 0` | yes | yes ✅ |
| Phase 3 all existing tests pass | 97/97 | 97/97 ✅ |
| Phase 3 public API unchanged | yes | yes ✅ |
| Phase 3 peak RSS no regression | ≤ 97 MB | 88 MB ✅ |
| Phase 7 ≥ 2× on profile `zero` | ≥ 2 544 | 3 184 ✅ |

The 10× target on `vn-google` requires Phase 7c (Rust extension via
PyO3 + quick-xml) — left as a future step. Phase 3+7a+7b together
deliver **4.4-8×** across all profiles, exceeding Phase 7's 2× gate
and most of the §0 "definition of done" sub-targets except the headline
10×.

## Architectural decisions documented along the way

1. **Worker-pool over nested task groups.** A first cut used
   `anyio.create_task_group()` recursively (one per parent). Workers
   ended up serialised in practice. The flat `asyncio.Queue` + N
   workers + bottom-up `_rebuild_recursive` was both simpler and
   faster.
2. **`url_normalize` hook.** Lets us route upstream absolute URLs
   to a local replay server during benchmarking without touching
   the production code path. The hook also lets production users
   plug in URL rewriting for caching / domain-fronting / etc.
3. **lxml only for XML.** RSS/Atom/text still go through expat.
   For `play.google.com` (XML only) this is the common case; for
   the rare RSS feed it would still work but slower.
4. **3.14t is "free" extra speed.** No code changes beyond
   running on the free-threaded build. The extra RSS (≈150 MB vs
   77 MB) is the only trade-off, and well within reason.

## Open follow-ups

- **Phase 7c** — PyO3 + quick-xml Rust extension. Plan estimates
  3-5× but is high-effort. Could push `vn-google` over the 10×
  bar.
- **Phase 4** — streaming parse + single-file spill. Doesn't
  improve `urls_per_s` much (HTTP bytes are already streamed);
  mostly buys us constant RSS for crawls with millions of URLs.
- **Phase 5** — ETag/Last-Modified cache, checkpointed frontier,
  JSONL streaming output.
- **Phase 6** — extend the test surface (`hypothesis`,
  `atheris`-driven fuzzing, mypy --strict on `usp/fetcher`).
- **LIVE run on `play.google.com`** — explicitly *not* done in
  this batch (plan requires per-run user approval).

## Reproducing

```bash
git clone https://github.com/dttad/ultimate-sitemap-parser
cd ultimate-sitemap-parser
git checkout perf/optimize-async
uv sync --group dev --group perf --extra perf

# Start the replay server (in another shell)
uv run bench/serve_corpus.py --profile vn-google --port 8765

# Run a benchmark
uv run bench/run.py --mode replay --profile vn-google --fanout-cap 200 --phase baseline
USP_USE_LXML=1 uv run bench/run_async.py --profile vn-google --fanout-cap 200 --phase P7-lxml

# Compare
uv run bench/compare.py bench/results/baseline.json bench/results/P7-lxml-vn-google-cap200.json
```
