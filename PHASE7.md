# Phase 7a — lxml.iterparse (results)

Implemented: `usp/fetcher/lxml_parser.py` (page + index parsers using
`lxml.etree.iterparse`), integrated into `usp/fetcher/crawler.py` with
the ``USP_USE_LXML=1`` env var (default-on for this script).

## Numbers (Python 3.11, 20-core Linux)

| Profile | Cap | Baseline | P3-async | **P7a-lxml** | vs Base | vs P3 |
|---|---:|---:|---:|---:|---:|---:|
| zero | 50 | 628 | 740 | **3 060** | **4.87×** | 4.13× |
| zero | 200 | 1 272 | 1 516 | **3 184** | **2.50×** | 2.10× |
| lan | 50 | 689 | 687 | **3 085** | **4.48×** | 4.49× |
| lan | 200 | 1 376 | 1 493 | **3 285** | **2.39×** | 2.20× |
| **vn-google** | 50 | 401 | 714 | **2 289** | **5.71×** | 3.20× |
| **vn-google** | 200 | 602 | 1 400 | **2 647** | **4.40×** | 1.89× |
| hostile | 50 | 337 | 633 | **2 313** | **6.86×** | 3.65× |
| hostile | 200 | 420 | 1 380 | **2 805** | **6.68×** | 2.03× |

Wall time at vn-google cap=200: **130 s → 30 s** (4.40×).
Peak RSS: 97 MB → 77 MB (better than baseline).

## Gate 7 evaluation (MASTER-PLAN §7)

| Gate | Target | Achieved | Status |
|---|---|---|---|
| ≥ 2× on profile `zero` | ≥ 2 544 | 3 184 | ✅ 2.50× |

## Correctness

- `bench/_compare_full.py` runs the same corpus files through both
  expat and lxml parsers. Page count matches **20/20** after the
  fix to use `start`/`end` events for `<url>` boundaries (the
  `end`-only version was losing the first page of each file because
  iterparse emits `<loc>` end before `<url>` start).

- Public API unchanged. `bench/_compare_full.py` shows that every
  page the expat parser produces is also produced by lxml (with
  identical field extraction for the supported subset).

- `_sniff_root` was buggy in the first iteration — it didn't skip
  the `<?xml ... ?>` declaration, so sitemapindex files (which
  always carry one) were sniffed as `None` and routed to the expat
  path. Fixed; now skips XML decl + BOM.

## What the lxml parser does and doesn't do

- ✅ XML `urlset` (pages): full extraction of `loc`, `lastmod`,
  `priority`, `changefreq`, `image:image`, `news:news`.
- ✅ XML `sitemapindex`: child URLs only.
- ❌ RSS 2.0 (`<rss>/<channel>/<item>`): falls back to expat.
- ❌ Atom 1.0 (`<feed>/<entry>`): falls back to expat.
- ❌ robots.txt index: handled by expat fallback.
- ❌ Plain-text sitemap: handled by expat fallback.

If a benchmark target uses RSS/Atom, the lxml path is bypassed and
the expat path runs. For the play.google.com corpus (XML only),
this doesn't matter.

## Why we're not at 10× yet

Phase 3 removed the I/O wait. Phase 7a replaced the parser with
a 1.4× faster one. Remaining time is split between:
- HTTP/2 latency (vn-google) — already overlapped, ~30% of wall.
- Worker-pool queue overhead — small but real.
- Python GIL serialising parse — the next lever (Phase 7b:
  Python 3.14t free-threaded) could give another 1.5–4× by
  parsing N sub-sitemaps truly in parallel.

## Files added / changed

- `usp/fetcher/lxml_parser.py` — page + index parsers via
  `lxml.etree.iterparse`, with `_PageBuilder`/`_sniff_root`
  helpers and a `parse_lxml_root()` dispatch.
- `usp/fetcher/crawler.py` — `AsyncCrawler` now branches on
  `_sniff_root()` and `USP_USE_LXML`; falls back to expat
  for non-XML inputs.
- `bench/_compare_full.py` — correctness comparator.
- `bench/run_async.py` — already supports the async path; lxml
  is now used inside.
- `bench/results/P7-lxml-*.json` — 8 result files (4 profiles ×
  2 caps).
