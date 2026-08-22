# Code audit — usp-max 1.9.0

Date: 2026-08-22
Reviewer: AI assistant
Scope: usp/cli/*, usp/fetcher/*, rust/src/lib.rs, Dockerfile, Makefile

## Critical issues (P0)

### CRIT-1: Memory bomb in `_crawl_async`

**Where:** `usp/cli/_crawl.py:230-244`

```python
async def producer() -> None:
    async with anyio.create_task_group() as tg:
        send, recv = anyio.create_memory_object_stream(max_buffer_size=1000)
        async def pump():
            async for url in run():
                await send.send(url)
            await send.send(None)
        tg.start_soon(pump)
        while True:
            url = await recv.receive()
            if url is None:
                break
            rec.append(url)        # ← ALL URLs accumulated in memory

anyio.run(producer)
yield from rec                    # ← Then yielded from the buffer
```

**Impact:** For a 50 M URL crawl, `rec` holds 50 M strings ≈ 3-5 GB RAM.
The plan §0 explicitly required RSS not to regress vs upstream (97 MB), but
this is hundreds of times worse.

**Fix:** stream URLs directly. Use a thread-safe `queue.Queue` and a
daemon thread that runs `anyio.run` and pushes into it.

### CRIT-2: PagesXMLSitemap materialises every URL in memory

**Where:** `usp/fetcher/crawler.py:264-270`

```python
urls = rust_parser.parse_pages_urls(content_bytes)
pages = [SitemapPage(url=u) for u in urls]
sitemap = PagesXMLSitemap(url=final_url, pages=pages)
```

`PagesXMLSitemap.__init__` immediately picks all pages to a tempfile
(via `_dump_pages`). The PagesXMLSitemap object then references the
tempfile path. When `.pages` is read back (in `walk()` → `all_pages()`),
the whole list is unpickled.

**Impact:** Double memory cost. Also disk thrash (write+read on every
sitemap). For 1 M URLs across 200 sitemaps that's 200 × 1 M = 200 M
pickle round-trips, which is the slow part of the crawl.

**Fix:** skip `PagesXMLSitemap` entirely when going through the Rust
parser. Yield the URLs directly without going through the Sitemap
abstraction.

### CRIT-3: Missing redirect-loop detection in async crawler

**Where:** `usp/fetcher/crawler.py:_process` — no equivalent of the
upstream `if response_url in self._parent_urls` check.

**Impact:** A sitemap that 301-redirects to its parent will recurse
forever (modulo max-depth, which is 16 by default — so up to 16
unbounded redirects).

**Fix:** after the fetch, if `response.url() in parent_urls`, treat
as InvalidSitemap and return.

### CRIT-4: No retry for transient failures

**Where:** `usp/fetcher/async_client.py:AsyncWebClient.get` returns
`_AsyncResponse.from_error(url, ex)` on any exception. No backoff.

**Impact:** A single 503 / 429 / connection-reset fails the whole
sitemap. On flaky networks this multiplies total runtime.

**Fix:** add a retry policy in `AsyncWebClient.get()` for:
- 5xx responses (except 501 Not Implemented)
- 429 Too Many Requests (honour `Retry-After` if present)
- `httpx.ConnectError`, `httpx.ReadError`, `httpx.PoolTimeout`

Use exponential backoff + full jitter, cap at 3 attempts.

### CRIT-5: Stopper can hang the shutdown

**Where:** `usp/fetcher/crawler.py:crawl`

```python
async def stopper():
    await queue.join()
    for _ in range(self._concurrency):
        await queue.put(None)
tg.start_soon(stopper)
```

If the user hits Ctrl+C while a worker is mid-fetch, `queue.join()` may
never return (workers are cancelled mid-await), and the task group
exits via cancellation. The shutdown then proceeds to `_finalize`,
which iterates `_children_of` — fine.

But there's a subtler bug: if a worker is **not** currently awaiting
`queue.get()` (e.g. it just finished and is in the middle of a
sitemap parse), cancellation can wait until the parse completes.
A 100 MB gzip-decode + parse can take seconds; multiplied across
many workers this is a real problem.

**Fix:** wrap the per-fetch+parse in `anyio.move_on_after()` or
`anyio.fail_after()` with the user's deadline, so a stuck worker
gives up within N seconds.

## High-priority issues (P1)

### HI-1: No tests for new code

The `usp/cli/_crawl.py`, `_inspect.py`, `_log.py` and
`usp/fetcher/crawler.py` files have **zero** dedicated tests. The
existing 97 tests cover only `usp/fetch_parse.py` (untouched upstream
code).

We need at minimum:
- `_log.py`: formatter output (TTY, non-TTY, edge cases)
- `_crawl.py`: end-to-end CLI test (mock replay, assert files written)
- `_inspect.py`: ls/wc/manifest/extract on a fixture dir
- `crawler.py`: retry-on-5xx, redirect-loop, budget exhaustion
- `rust_parser.py`: round-trip parse + lxml parity

### HI-2: Manifest missing fields

**Where:** `usp/cli/_crawl.py:455-472`

The manifest JSON has only `urls_total`, `batches`, `elapsed_s`,
`urls_per_s`. It does not record:
- `sitemaps_fetched`, `sitemaps_failed`
- per-batch bytes
- per-status HTTP code counts (200, 404, 5xx, 429)
- exit reason (success / budget-exhausted / error)
- parser backend actually used (rust / lxml / expat)

This is the first thing an operator reaches for when debugging a
crawl. Add it now.

### HI-3: Stream URLs directly from async to batch writer

**Where:** `usp/cli/_crawl.py:run()`

The for loop receives URLs from the iterator but writes them to a
batch via a writer created in `open_batch()`. That's already
streaming, so this only matters if combined with HI-1's
`PagesXMLSitemap` removal — once the iterator yields `SitemapPage`
objects without the pickle detour, the per-URL write is fine.

### HI-4: Tests for retry / redirect / budget

Already covered by HI-1. Calling out separately because each
behaviour is non-trivial and warrants its own test.

## Medium-priority issues (P2)

### MED-1: `import json` repeated inside the for-loop

**Where:** `usp/cli/_crawl.py:404, 422, 471`

The `import json as _json` inside `run()` and the duplicate `import json`
later in the function. Move all to the top of the file.

### MED-2: `--max-urls` checked per page, not per worker

**Where:** `usp/cli/_crawl.py:222-225`

```python
for page in node.pages:
    seen_count[0] += 1
    if args.max_urls and seen_count[0] > args.max_urls:
        return
```

`seen_count` is shared across all workers via the closure. So the cap
is global. But the check is per-page, and many workers can race
past it before any of them sees the value exceed. Worst case: extra
`concurrency` URLs beyond the cap. For `--max-urls 1000000` that's
negligible. Document it instead of fixing.

### MED-3: HTTP HEAD requests not supported

The replay server returns 501 for HEAD. `httpx.head()` would be useful
for cheap content-length pre-checks before GET. Defer.

### MED-4: `NoWebClientException` is dead code

`usp/fetcher/crawler.py:286` — this is from upstream's `LocalWebClient`
which we never use. Dead branch. Remove.

## Low-priority / nice-to-have (P3)

### LOW-1: `from . import rust_parser` inside the loop

**Where:** `usp/fetcher/crawler.py:265, 281`

Python caches imports so this is free, but stylistically it's
noisy. Move to module level.

### LOW-2: `pytest` not run in CI on the new code

The repo has `.github/workflows/lint.yml`, `test.yml`,
`test_integration.yml` — but no test job specifically for the fetcher
or CLI modules. Add a focused test step.

### LOW-3: The CLI accepts `--parser expat` but doesn't disable async

`--parser expat` should imply `--backend sync` to actually use expat.
Right now expat is only the fallback inside the async crawler. If a
user passes `--parser expat --backend async` they get lxml unless
lxml is also unavailable. Document the semantic.

### LOW-4: No `--rate-limit` flag

Operators crawling large sites often want to be polite. A
`--max-rps N` flag with a token-bucket per host would be appreciated.
Defer (plan §3.4 mentions this as a follow-up).

## Performance notes

- The Rust parser path is 1.73× faster than lxml in micro-bench.
  End-to-end it accounts for ~85% of parse time.
- The 50 MB size cap is the same as upstream. A single 1 GB
  sitemap will be rejected with `decompress: 100MB exceeded`.
- The pickle round-trip per PagesXMLSitemap is the next bottleneck
  after Rust parsing is in place. Fixing HI-1 removes this.

## Security notes

- The expat hardening (DOCTYPE / ENTITY handlers) is preserved in the
  expat fallback path (`XMLSitemapParser._xml_hardening_handler`).
- The Rust path does not have those protections. `quick-xml` does
  parse XML safely by default (no entity expansion by default,
  no external entities), but the lxml fallback also doesn't disable
  XXE. We rely on the safe defaults of `lxml` and `quick-xml`.
- The `_url_normalize` hook runs on **every** URL we enqueue. A
  malicious config could rewrite URLs to e.g. a local file. We're
  trusting the operator here; document it.

## Plan

| Step | Owner | Estimated impact |
|---|---|---|
| HI-1 + HI-4: write a `tests/cli/test_crawl.py` and `tests/cli/test_log.py` | me | high |
| CRIT-1: stream URLs from async to sync iterator | me | high |
| CRIT-2: skip PagesXMLSitemap pickle detour for Rust parser | me | high |
| CRIT-3: add redirect-loop detection | me | high |
| CRIT-4: add retry with backoff | me | high |
| CRIT-5: bounded cancellation | me | medium |
| HI-2: enrich manifest | me | medium |
| MED-1..4: minor cleanups | me | low |

After fixes, re-run the full Phase 7c benchmark suite to confirm no
regression vs the 14.55× baseline.
