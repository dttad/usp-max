# usp-max

> High-performance fork of [`GateNLP/ultimate-sitemap-parser`](https://github.com/GateNLP/ultimate-sitemap-parser)
> with **async I/O + Rust parser**, delivering **14.55× the speed of upstream** on real-world workloads.
> Drop-in compatible: same Python API, same XML semantics, just faster.

[![Python](https://img.shields.io/pypi/pyversions/ultimate-sitemap-parser)](https://pypi.org/project/ultimate-sitemap-parser/)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE.txt)
[![Tests](https://img.shields.io/badge/tests-97%2F97-brightgreen.svg)](#testing)
[![Speed](https://img.shields.io/badge/vs%20upstream-14.55x-orange.svg)](#performance)

## What is this?

`usp-max` is a maintained fork of `ultimate-sitemap-parser` (USP) for
operators who need to crawl **millions to billions of URLs** out of
sitemap trees without paying the upstream price of:

- **Sequential fetching** (one HTTP request at a time per sitemap)
- **Python overhead in the parser** (expat callbacks, regex routing, `str.split` on every element)
- **No concurrency model** — the original API is purely sync and recursive

`usp-max` ships three independent optimizations that compose:

| Layer | What | Speedup vs upstream |
|---|---|---|
| **Phase 3** — Async crawler | `httpx[http2]` + `anyio` worker pool; inverted control flow | 2.33× |
| **Phase 7a** — `lxml.iterparse` | Drop-in `lxml` backend (1.27× faster than expat) | 4.40× |
| **Phase 7b** — CPython 3.14t | Free-threaded build; concurrent parse | 4.88× |
| **Phase 7c** — Rust + quick-xml | `usp_fast` PyO3 extension (21.7× faster than expat per thread) | **14.55×** |

All four layers are **opt-in** and fall back gracefully:

| Setting | Behaviour |
|---|---|
| `USP_USE_RUST=1` (default) | Try Rust, fall back to lxml, fall back to expat |
| `USP_USE_LXML=1` (default) | Try lxml, fall back to expat |
| `USP_USE_RUST=0` and `USP_USE_LXML=0` | Pure upstream code path (still works with async httpx) |

The public Python API is **unchanged**. `sitemap_tree_for_homepage(...)`
returns the exact same `AbstractSitemap` tree it always did. New
capabilities (async crawler, batched CLI, Rust backend) are
**additive**.

## Performance (vn-google cap=200, replay corpus, Python 3.12, single node)

```
profile      cap   baseline   P3-async   P7a-lxml   P7b-3.14t   P7c-Rust
zero         50      628      740       3 060      3 275     16 510
zero        200    1 272    1 516       3 184      3 403     20 907
lan          50      689      687       3 085      3 329     17 838
lan         200    1 376    1 493       3 285      3 358     21 623
vn-google    50      401      714       2 289      2 501      7 427
vn-google   200      602    1 400       2 647      2 936      8 767   <-- 14.55x
hostile      50      337      633       2 313      2 688      6 501
hostile     200      420    1 380       2 805      3 074      8 967
```

- **vn-google cap=200**: 130 s → 9 s (14.55×)
- **zero cap=50** (CPU-bound): 31 s → 1.2 s (**26.3×**)
- All 97 existing tests pass; public API unchanged.

See [`bench/results/FINAL-REPORT.md`](bench/results/FINAL-REPORT.md)
for the full report (profile definitions, single-thread parse
benchmarks, architectural decisions, gate evaluation).

## Features

- **All upstream features** (XML / News / Image / RSS / Atom / robots.txt /
  plain-text sitemaps; permissive XML parsing; custom web client).
- **Async crawler** with HTTP/2 multiplexing via `httpx[http2]`.
- **`lxml.iterparse` backend** for 1.3-2× parser speedup.
- **Rust+quick-xml extension** for another 4-5× parser speedup.
- **CPython 3.14t free-threaded** support for true parallel parsing.
- **`usp-max crawl` CLI** with batched, compressed, tarred output.
- **Single Docker image** that bundles Python + Rust extension + all
  optional dependencies.

## Quick start

### As a library (drop-in for `ultimate-sitemap-parser`)

```python
from usp.tree import sitemap_tree_for_homepage

tree = sitemap_tree_for_homepage("https://example.com/")
for page in tree.all_pages():
    print(page.url)
```

If you installed `usp-max` with the `[fast]` extra, the async crawler
is automatically used when you set `USP_USE_RUST=1` (default).

### As a CLI / Docker (production use)

```bash
# Build and run locally (one command per crawl)
usp-max crawl https://play.google.com/ \
    -o /data/urls \
    --batch-size 50000 \
    --compress zstd \
    --tar \
    --concurrency 32 \
    --fanout-cap 500
```

Output:
```
/data/urls/
  urls-00001.txt.tar.zst    # 50 000 URLs, zstd-compressed, in a tar
  urls-00002.txt.tar.zst
  ...
  manifest.json             # run summary (urls_total, rate, ...)
```

Extract:
```bash
zstd -d urls-00001.txt.tar.zst --stdout | tar -xOvf - urls-00001.txt
```

### Docker (recommended for production)

The image bundles Python + Rust extension + all optional dependencies.
Build and run:

```bash
# Build locally
docker build -t usp-max:1.9.0 .

# One-shot crawl with bind-mounted output directory
docker run --rm \
    --network host \                # so the container can reach the target
    -v /data/urls:/home/uspmax/out \
    usp-max:1.9.0 \
    crawl https://play.google.com/ \
        -o /home/uspmax/out \
        --batch-size 50000 \
        --compress zstd --tar \
        --concurrency 32 \
        --fanout-cap 500
```

Image properties (after `docker images usp-max:1.9.0`):

```
REPOSITORY  TAG    SIZE
usp-max     1.9.0  ~250 MB     # Python 3.12-slim + httpx + lxml + usp_fast.so
```

The image:
- runs as a non-root user (`uspmax`, uid 1000)
- has `/install/bin/python` on PATH (everything is in `/install`)
- ships `usp_fast` (PyO3+quick-xml) pre-compiled with abi3 so it works on
  Python 3.11/3.12/3.13/3.14t
- ENTRYPOINT is `python -m usp.cli_main`; CMD defaults to `crawl --help`

## Installation

### From source (recommended for the Docker build)

```bash
git clone https://github.com/dttad/usp-max
cd usp-max

# Python venv with all the bells and whistles
uv venv .venv
uv pip install -e '.[fast]'    # httpx + anyio + lxml + maturin (Rust build below)

# Build the Rust extension (one-time, requires cargo)
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 .venv/bin/maturin develop --release
```

### Pre-built wheel (when we publish one)

```bash
pip install 'usp-max[fast]'
```

### From a Docker image (after build)

```bash
docker run --rm -v $(pwd)/out:/out ghcr.io/dttad/usp-max:latest \
    crawl https://example.com/ \
    -o /out \
    --batch-size 50000 \
    --compress zstd --tar
```

## CLI reference

```
usp-max crawl <URL> [OPTIONS]
```

### Required

| Argument | Description |
|---|---|
| `URL` | Homepage URL to start from (e.g. `https://example.com/`) |

### Output

| Flag | Default | Description |
|---|---|---|
| `-o`, `--output DIR` | `./usp-max-out` | Output directory |
| `--batch-size N` | `10000` | URLs per batch file |
| `--format {txt,jsonl}` | `txt` | `txt` = one URL per line; `jsonl` = `{"url": "..."}` per line |
| `--compress {none,gz,zstd}` | `none` | Per-file compression |
| `--tar` | off | Wrap each batch in a tar archive (stacks with `--compress`) |

Filename pattern: `urls-NNNNN.{txt|jsonl}[.tar][.gz|.zst]`

### Crawl control

| Flag | Default | Description |
|---|---|---|
| `--concurrency N` | `16` | Worker pool size for fetches |
| `--fanout-cap N` | `200` | Max sub-sitemaps per index level |
| `--max-depth N` | `16` | Max sitemap recursion depth |
| `--deadline-seconds N` | `0` | Stop after N seconds (0 = no deadline) |
| `--max-urls N` | `0` | Stop after N URLs (0 = no limit) |
| `--max-sitemaps N` | `0` | Stop after N sitemaps fetched (0 = no limit) |
| `--user-agent STRING` | `usp-max/1.9.0 (+https://github.com/dttad/usp-max)` | HTTP UA |

### Backend

| Flag | Default | Description |
|---|---|---|
| `--backend {sync,async}` | `async` | Crawler backend (sync uses upstream `SitemapFetcher`) |
| `--parser {expat,auto}` | `auto` | `auto` = Rust → lxml → expat; `expat` = force sync |

### Logging

| Flag | Description |
|---|---|
| `-q`, `--quiet` | Suppress progress logs |
| `-v`, `--verbose` | DEBUG-level logging |
| `--progress-json FILE` | Append progress events as JSONL |

### Example invocations

```bash
# 1) Default: txt batches of 10k URLs
usp-max crawl https://example.com/

# 2) Larger batches, zstd + tar, more workers
usp-max crawl https://example.com/ \
    -o /data/run-$(date +%Y%m%d) \
    --batch-size 100000 \
    --compress zstd --tar \
    --concurrency 64 --fanout-cap 500

# 3) Bounded run (stop after 1 hour or 10M URLs)
usp-max crawl https://example.com/ \
    --deadline-seconds 3600 --max-urls 10000000 \
    --progress-json /var/log/run.jsonl

# 4) Sync / expat fallback (no extra deps, original API)
usp-max crawl https://example.com/ --backend sync --parser expat
```

## Architecture

```
                            usp-max fork
                            ────────────
   ┌──────────────────────────────────────────────────────────────┐
   │  usp/cli/_crawl.py    CLI:  usp-max crawl URL [opts]            │
   │       │              stream URLs to disk as batched files      │
   │       ▼                                                       │
   │  usp/fetcher/crawler.py   AsyncCrawler (asyncio worker pool)   │
   │       │            ┌── dedupe, budget, semaphore              │
   │       │            └── bottom-up rebuild of IndexXMLSitemap    │
   │       ▼                                                       │
   │  Parser cascade (3-tier fallback):                            │
   │       1. usp_fast (Rust+quick-xml) — fastest, requires build  │
   │       2. lxml.etree.iterparse  — fast, pip-installable        │
   │       3. expat (Python stdlib) — always available             │
   │       ▼                                                       │
   │  usp/fetcher/async_client.py   httpx[http2] AsyncWebClient    │
   │       │            gzip-aware, returns abstract response       │
   │       ▼                                                       │
   │  HTTP/2 over TCP — multiplexed                                 │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  usp/tree.py   (UNCHANGED — original recursive SitemapFetcher) │
   │  usp/fetch_parse.py   (UNCHANGED — original expat parser)       │
   │  usp/objects/*   (UNCHANGED — domain types)                   │
   │  Original public API:  sitemap_tree_for_homepage(...)         │
   └──────────────────────────────────────────────────────────────┘
```

### Key invariants

- `usp.tree.sitemap_tree_for_homepage` is **byte-for-byte
  equivalent** to upstream (modulo crawl speed).
- All 97 upstream tests pass.
- License stays **GPL-3.0-or-later**.
- Expat hardening (DOCTYPE / ENTITY handlers,
  `SetParamEntityParsing(NEVER)`, 100 MB size cap) is preserved in
  the expat path.
- Rust backend handles the same XML subset as expat: `<urlset>` /
  `<sitemapindex>`. RSS / Atom / plain-text still go through expat.

## Repository layout

```
usp-max/
├── usp/                       # the package (drop-in fork)
│   ├── tree.py                # public sync API — UNCHANGED
│   ├── fetch_parse.py         # expat parser — UNCHANGED
│   ├── objects/               # domain types — UNCHANGED
│   ├── web_client/            # sync web client — UNCHANGED
│   ├── fetcher/               # NEW: async / parallel / multi-parser
│   │   ├── async_client.py    #   httpx[http2] async client
│   │   ├── crawler.py         #   worker-pool AsyncCrawler
│   │   ├── lxml_parser.py     #   lxml.iterparse backend (Phase 7a)
│   │   └── rust_parser.py     #   usp_fast wrapper (Phase 7c)
│   └── cli/
│       ├── cli.py             # legacy `usp ls`
│       ├── _ls.py             # legacy ls command
│       └── _crawl.py          # NEW: `usp-max crawl` command
├── usp/cli_main.py            # NEW: usp-max entry point
├── rust/                      # Phase 7c: PyO3 + quick-xml
│   ├── Cargo.toml
│   └── src/lib.rs             # parse_pages, parse_index
├── bench/                     # benchmark harness
│   ├── corpus/               #   250 gzipped sub-sitemaps + 2 indexes
│   ├── serve_corpus.py        #   local replay server
│   ├── fetch_corpus.py        #   corpus fetcher
│   ├── run.py                 #   sync benchmark runner
│   ├── run_async.py           #   async benchmark runner
│   ├── compare.py             #   regression gate
│   ├── aggregate.py           #   baseline aggregator
│   ├── profile.py             #   pyinstrument / memray driver
│   └── results/               #   PROFILE.md, PHASE*.md, FINAL-REPORT.md, *.json
├── Dockerfile                 # multi-stage: rust build + slim runtime
├── pyproject.toml             # optional-dependencies: async / lxml / rust / fast
├── CLAUDE.md                  # workflow rules for AI agents
└── MASTER-PLAN-usp-perf.md    # the original optimization plan (all gates met)
```

## Benchmarking (for AI agents)

Re-running benchmarks takes a couple of minutes and produces JSON +
Markdown reports you can diff between branches:

```bash
# Start the replay server (one terminal)
.venv-rust/bin/python bench/serve_corpus.py --profile vn-google --port 8765 &

# Run all 8 (profile × cap) combinations and write to bench/results/
.venv-rust/bin/python - <<'PY'
import subprocess, sys
for prof in ("zero", "lan", "vn-google", "hostile"):
    for cap in (50, 200):
        subprocess.check_call([
            ".venv-rust/bin/python", "bench/run_async.py",
            "--profile", prof, "--fanout-cap", str(cap),
            "--concurrency", "16",
            "--name", f"mychange-{prof}-cap{cap}",
            "--phase", "mychange",
        ])
PY

# Compare to baseline
.venv-rust/bin/python bench/compare.py \
    bench/results/baseline.json \
    bench/results/mychange-vn-google-cap200.json
```

`bench/compare.py` exits non-zero on regression >5% (urls/s) or >10%
(peak RSS).

## Development

### Testing

```bash
uv run pytest                           # 97 tests, ~2s
.venv-rust/bin/python -m pytest         # same, in the rust venv
```

### Lint / format

```bash
uv run ruff check --fix
uv run ruff format
```

### Rebuilding the Rust extension after a change

```bash
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 .venv-rust/bin/maturin develop --release
```

The wheel is built with `abi3` so the same artifact runs on Python
3.11 / 3.12 / 3.13 / 3.14t.

### Docker image

```bash
docker build -t usp-max:dev .
docker run --rm -v $(pwd)/out:/out usp-max:dev \
    crawl https://example.com/ \
    -o /out --batch-size 100000 --compress zstd --tar
```

## License

GPL-3.0-or-later (same as upstream). See [`LICENSE.txt`](LICENSE.txt)
and [`NOTICE`](NOTICE).

## Credits

- Upstream: [`GateNLP/ultimate-sitemap-parser`](https://github.com/GateNLP/ultimate-sitemap-parser)
  by Linas Valiukas, Hal Roberts, Freddy Heppell, and contributors.
- This fork (`usp-max`): maintained at
  [`dttad/usp-max`](https://github.com/dttad/usp-max).
- See [`bench/results/FINAL-REPORT.md`](bench/results/FINAL-REPORT.md)
  for the optimization journey (Phase 0 → Phase 7c).