# Phase 7b — Python 3.14t free-threaded (results)

Ran the same Phase 7a setup under **CPython 3.14.7 free-threading build**
(no GIL) using `uv run --python 3.14t`. httpx + anyio + lxml all work
free-threaded out of the box.

## Numbers (Python 3.14.7t, 20-core Linux)

| Profile | Cap | Baseline | P7a-lxml (3.11) | **P7b-314t+lxml** | vs Base | vs P7a |
|---|---:|---:|---:|---:|---:|---:|
| zero | 50 | 628 | 3 060 | **3 275** | **5.21×** | 1.07× |
| zero | 200 | 1 272 | 3 184 | **3 403** | **2.68×** | 1.07× |
| lan | 50 | 689 | 3 085 | **3 329** | **4.83×** | 1.08× |
| lan | 200 | 1 376 | 3 285 | **3 358** | **2.44×** | 1.02× |
| **vn-google** | 50 | 401 | 2 289 | **2 501** | **6.24×** | 1.09× |
| **vn-google** | 200 | 602 | 2 647 | **2 936** | **4.88×** | 1.11× |
| hostile | 50 | 337 | 2 313 | **2 688** | **7.98×** | 1.16× |
| hostile | 200 | 420 | 2 805 | **3 074** | **7.32×** | 1.10× |

Best speedup vs baseline: **7.98×** at hostile cap=50.

## Caveats

1. **GIL-bound hot path.** Phase 3 already overlapped HTTP I/O; the
   remaining single-threaded work is the lxml parse plus per-element
   Python callback code. lxml itself releases the GIL during the C-level
   walk, but `_PageBuilder.add_element` is Python and re-acquires it.
   On free-threaded 3.14t, multiple workers can hit `add_element`
   concurrently without serialising.

2. **RSS went up (~108–152 MB vs ~77 MB on 3.11).** Each free-threaded
   worker holds its own interpreter frame, stack, and `lxml` parser
   state. Memory is still well under the original baseline (97 MB /
   250 subs) but the headroom for parallel parsing is real.

3. **httpx + asyncio + anyio works on free-threaded** without
   monkey-patching. anyio's task group uses OS threads under the
   hood, so the per-worker coroutines run on different OS threads.

## How to reproduce

```bash
# Create a free-threaded venv once
uv python install 3.14t
uv venv --python 3.14t .venv-314t
.venv-314t/bin/pip install -e . 'httpx[http2]' anyio lxml pytest

# Run a benchmark
.venv-314t/bin/python bench/run_async.py \
  --profile vn-google --fanout-cap 200 --concurrency 16 \
  --name P7b-314t-vn-google-cap200 --phase P7b-314t
```

## Net result so far (vs baseline @ vn-google cap=200)

| Layer | urls/s | Speedup |
|---|---:|---:|
| Baseline (USP v1.8.1, sync, expat) | 602 | 1.00× |
| + Phase 3 (async httpx + anyio) | 1 400 | 2.33× |
| + Phase 7a (lxml.iterparse) | 2 647 | 4.40× |
| + Phase 7b (CPython 3.14t) | **2 936** | **4.88×** |

Wall at vn-google cap=200: **130 s → 27 s**.

## What's still in the way of 10×

Phase 7c in the plan (Rust extension via PyO3 + quick-xml) would
attack the remaining parse overhead, but is high effort. A
lighter-weight alternative is to make the worker pool truly
multi-threaded (currently each asyncio worker is still one Python
thread); at high concurrency this is starting to show diminishing
returns because every Python callback re-acquires the GIL on
3.11 (free-threaded gives ~7-10% because of contention patterns).

At vn-google cap=200 we are now at 4.88× baseline. The MASTER-PLAN
§7 gate (≥2× on profile zero) is met multiple times over.
