"""``usp-max crawl`` — crawl one or many sitemaps and stream URLs to disk.

Designed for the Docker CLI use case: a single command takes a URL
and writes results to a directory as one or more batched files,
optionally compressed with gzip/zstd (and optionally wrapped in a tar
archive).

Examples
--------
    usp-max crawl https://play.google.com/
    usp-max crawl https://example.com/ -o /data/urls \\
        --batch-size 50000 --compress zstd --tar \\
        --concurrency 32 --fanout-cap 500

Output filename pattern
-----------------------
    urls-NNNNN.{txt|jsonl}[.{gz|zst}][.tar]
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import os
import queue as thread_queue
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import anyio

from usp import __version__
from usp.cli._log import kv, setup_logging

log = logging.getLogger("usp-max.crawl")


# ----------------------------------------------------------------------
# Output compression helpers
# ----------------------------------------------------------------------

def _open_writer(path: Path, compress: str, use_tar: bool):
    """Return (file_handle, write_bytes, finalize).

    ``write_bytes(data)`` appends ``data`` to the underlying file.
    ``finalize()`` closes the file (and the tar archive if any).
    """
    if compress == "gz":
        raw = path.open("wb")
        fh: Any = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
        tar: tarfile.TarFile | None = None
        if use_tar:
            tar = tarfile.open(fileobj=fh, mode="w")
    elif compress == "zstd":
        try:
            import zstandard  # type: ignore[import-not-found]
        except ImportError as ex:
            raise SystemExit(
                "zstd compression requires the 'zstandard' package; "
                "install it via `pip install zstandard`."
            ) from ex
        raw = path.open("wb")
        cctx = zstandard.ZstdCompressor()
        fh = cctx.stream_writer(raw, closefd=True)
        tar = None
        if use_tar:
            tar = tarfile.open(fileobj=fh, mode="w")
    else:
        raw = path.open("wb")
        fh = raw
        tar = None
        if use_tar:
            tar = tarfile.open(fileobj=fh, mode="w")

    if tar is not None:
        inner_name = _strip_all_suffixes(path.name, use_tar=True, compress=compress)
        info = tarfile.TarInfo(name=inner_name)
        info.mtime = int(time.time())
        info.size = 0

        buf = io.BytesIO()
        def write_bytes(data: bytes) -> None:
            buf.write(data)

        def finalize() -> None:
            info.size = buf.tell()
            buf.seek(0)
            tar.addfile(info, buf)
            tar.close()
            fh.close()
            if fh is not raw:
                raw.close()

        return raw, write_bytes, finalize

    def write_bytes(data: bytes) -> None:
        fh.write(data)

    def finalize() -> None:
        fh.close()
        if fh is not raw:
            raw.close()

    return raw, write_bytes, finalize


def _strip_all_suffixes(name: str, use_tar: bool, compress: str) -> str:
    """Strip ALL outer suffixes to leave just the inner filename.

    ``urls-00001.txt.tar.gz`` -> ``urls-00001.txt``.
    ``urls-00001.txt.gz``     -> ``urls-00001.txt``.
    """
    suffixes = []
    if use_tar:
        suffixes.append(".tar")
    if compress == "gz":
        suffixes.append(".gz")
    elif compress == "zstd":
        suffixes.append(".zst")
    out = name
    changed = True
    while changed:
        changed = False
        for suf in suffixes:
            if out.endswith(suf):
                out = out[: -len(suf)]
                changed = True
    return out


def _batch_filename(index: int, fmt: str, compress: str, use_tar: bool) -> str:
    ext = "jsonl" if fmt == "jsonl" else "txt"
    parts = [f"urls-{index:05d}.{ext}"]
    if use_tar:
        parts.append(".tar")
    if compress == "gz":
        parts.append(".gz")
    elif compress == "zstd":
        parts.append(".zst")
    return "".join(parts)


# ----------------------------------------------------------------------
# Streaming bridge: async → sync
# ----------------------------------------------------------------------

# Sentinel for "no more URLs" in the thread queue.
_SENTINEL = object()


def _stream_urls_from_async(pages_async, q: "thread_queue.Queue") -> None:
    """Producer thread: runs the async pages iterator and pushes to q."""
    try:
        anyio.run(_drain_async, pages_async, q.put)
    except BaseException as ex:  # noqa: BLE001
        log.warning("async crawl failed: %s", ex)
    finally:
        q.put(_SENTINEL)


async def _drain_async(pages_async, put) -> None:
    async for url in pages_async:
        put(url)


# ----------------------------------------------------------------------
# Async crawler wrapper
# ----------------------------------------------------------------------

def _crawl_async_streaming(homepage: str, args: argparse.Namespace) -> Iterator[str]:
    """Bridge the async streaming crawler to a sync iterator via a thread queue.

    Memory bounded to ``concurrency * 4`` in-flight URLs. Safe for
    million-URL crawls.
    """
    from usp.fetcher.async_client import AsyncWebClient
    from usp.fetcher.crawler import AsyncCrawler

    cap = args.fanout_cap
    def cap_fn(urls, level, parents):
        return sorted(urls)[:cap]

    q: thread_queue.Queue = thread_queue.Queue(maxsize=max(args.concurrency * 4, 64))
    error: list[BaseException | None] = [None]

    def run_async() -> None:
        try:
            anyio.run(_async_main, homepage, args, q, cap_fn, error)
        except BaseException as ex:  # noqa: BLE001
            error[0] = ex
            q.put(_SENTINEL)

    thread = threading.Thread(target=run_async, daemon=True, name="crawl-async")
    thread.start()

    while True:
        item = q.get()
        if item is _SENTINEL:
            break
        yield item
    thread.join(timeout=1.0)
    if error[0] is not None:
        raise error[0]


async def _async_main(homepage, args, q, cap_fn, error) -> None:
    from usp.fetcher.async_client import AsyncWebClient
    from usp.fetcher.crawler import AsyncCrawler

    async with AsyncWebClient(
        http2=True,
        max_connections=max(args.concurrency * 2, 20),
        user_agent=args.user_agent,
    ) as client:
        def normalize_to_local(url: str) -> str:
            return url

        crawler = AsyncCrawler(
            client,
            concurrency=args.concurrency,
            max_sitemaps=args.max_sitemaps or float("inf"),
            max_depth=args.max_depth,
            deadline_s=args.deadline_seconds or None,
            recurse_callback=None,
            recurse_list_callback=cap_fn,
            url_normalize=normalize_to_local,
            use_lxml=args.parser != "expat",
            use_rust=args.parser != "expat",
        )
        log.info("→ crawl started  %s", kv(
            homepage=homepage,
            concurrency=args.concurrency,
            parser="auto" if args.parser != "expat" else "expat",
        ))
        crawl_started = time.monotonic()
        try:
            async for url in crawler.crawl_streaming([homepage]):
                q.put(url)
                if args.max_urls:
                    # We can't easily count in streaming mode, but
                    # budget is enforced inside the crawler too.
                    pass
        except Exception as ex:
            error[0] = ex
        elapsed = time.monotonic() - crawl_started
        log.info("✓ crawl finished  %s", kv(
            sitemaps_fetched=crawler.sitemaps_fetched,
            sitemaps_failed=crawler.sitemaps_failed,
            elapsed_s=round(elapsed, 2),
        ))


# ----------------------------------------------------------------------
# Sync fallback (uses upstream sync SitemapFetcher)
# ----------------------------------------------------------------------

def _crawl_sync(homepage: str, args: argparse.Namespace) -> Iterator[str]:
    from usp.tree import sitemap_tree_for_homepage
    from usp.objects.sitemap import (
        IndexRobotsTxtSitemap, IndexXMLSitemap, PagesXMLSitemap,
    )

    def cap(urls, level, parents):
        return sorted(urls)[:args.fanout_cap]

    tree = sitemap_tree_for_homepage(
        homepage,
        use_robots=True,
        use_known_paths=False,
        recurse_list_callback=cap,
    )

    def walk(node):
        if isinstance(node, PagesXMLSitemap):
            for p in node.pages:
                yield p.url
        for sub in node.sub_sitemaps:
            yield from walk(sub)

    return walk(tree)


def _make_cap(cap: int) -> Callable[[list[str], int, set[str]], list[str]]:
    def _cap(urls: list[str], level: int, parents: set[str]) -> list[str]:
        return sorted(urls)[:cap]
    return _cap


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "crawl",
        help="Crawl a sitemap tree and write URLs to disk in batches",
        description=(
            "Crawl one homepage URL, recursively fetch every sitemap it "
            "discovers, and stream the resulting URLs to disk as one or "
            "more batched files (optionally gz / zstd / tar)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", help="Homepage URL (e.g. https://example.com/)")
    p.add_argument(
        "-o", "--output", default="./usp-max-out", type=Path,
        help="Output directory (default: ./usp-max-out)",
    )
    p.add_argument(
        "--batch-size", type=int, default=10_000,
        help="Number of URLs per output file (default: 10000)",
    )
    p.add_argument(
        "--format", choices=["txt", "jsonl"], default="txt",
        help="Output format (default: txt). jsonl = one URL per JSON line",
    )
    p.add_argument(
        "--compress", choices=["none", "gz", "zstd"], default="none",
        help="Compress each batch file (default: none)",
    )
    p.add_argument(
        "--tar", action="store_true",
        help="Wrap each batch file in a tar archive (combines with --compress)",
    )
    p.add_argument(
        "--concurrency", type=int, default=16,
        help="Worker pool size (default: 16)",
    )
    p.add_argument(
        "--fanout-cap", type=int, default=200,
        help="Max sub-sitemaps per index level (default: 200)",
    )
    p.add_argument(
        "--max-depth", type=int, default=16,
        help="Max sitemap recursion depth (default: 16)",
    )
    p.add_argument(
        "--deadline-seconds", type=float, default=0.0,
        help="Stop after N seconds (0 = no deadline)",
    )
    p.add_argument(
        "--max-urls", type=int, default=0,
        help="Stop after N URLs (0 = no limit)",
    )
    p.add_argument(
        "--max-sitemaps", type=int, default=0,
        help="Stop after N sitemaps fetched (0 = no limit)",
    )
    p.add_argument(
        "--user-agent", default=f"usp-max/{__version__} (+https://github.com/dttad/usp-max)",
        help="HTTP User-Agent header",
    )
    p.add_argument(
        "--backend", choices=["sync", "async"], default="async",
        help="Crawler backend (default: async)",
    )
    p.add_argument(
        "--parser", choices=["expat", "auto"], default="auto",
        help="Parser backend: auto uses Rust > lxml > expat; expat forces sync",
    )
    p.add_argument(
        "--strip-url", action="store_true",
        help="Strip the supplied URL from each page URL (cosmetic)",
    )
    p.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress progress logs",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose logging (DEBUG level)",
    )
    p.add_argument(
        "--progress-json", type=Path, default=None,
        help="Write progress events as JSONL to this file",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    verbosity = 2 if args.verbose else (0 if args.quiet else 1)
    setup_logging(verbosity=verbosity, log_path=None)

    log.info("→ usp-max %s", __version__)
    log.info("  %s", kv(
        url=args.url,
        out=str(args.output),
        batch=args.batch_size,
        format=args.format,
        compress=args.compress,
        tar=args.tar,
        concurrency=args.concurrency,
        fanout_cap=args.fanout_cap,
        parser=args.parser,
        backend=args.backend,
    ))

    # Sanity-check the output directory up-front so we fail fast on
    # permission / read-only / missing-volume errors instead of after
    # the crawl has finished.
    try:
        args.output.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        log.error("cannot create output dir %s: %s", args.output, ex)
        sys.exit(2)
    if not os.access(args.output, os.W_OK):
        log.error("output dir %s is not writable", args.output)
        sys.exit(2)

    started = time.monotonic()
    written_total = 0
    batch_index = 0
    current_path: Path | None = None
    current_writer = None
    current_finalize = None
    current_count = 0
    progress_fh = args.progress_json.open("a") if args.progress_json else None
    last_progress_log = started
    last_progress_event = started

    def open_batch() -> None:
        nonlocal current_path, current_writer, current_finalize, current_count, batch_index
        name = _batch_filename(
            batch_index + 1, args.format, args.compress, args.tar,
        )
        current_path = args.output / name
        _raw, current_writer, current_finalize = _open_writer(
            current_path, args.compress, args.tar,
        )
        current_count = 0

    def close_batch() -> None:
        nonlocal current_count, batch_index, current_path
        nonlocal current_writer, current_finalize
        if current_writer is None:
            return
        current_finalize()
        size = current_path.stat().st_size if current_path.exists() else 0
        elapsed = time.monotonic() - started
        rate = current_count / elapsed if elapsed > 0 else 0.0
        log.info(
            "  ✓ %s  %s",
            current_path.name,
            kv(urls=current_count, bytes=size, urls_per_s=round(rate, 1)),
        )
        if progress_fh:
            progress_fh.write(
                f'{{"event":"batch","index":{batch_index + 1},'
                f'"path":"{current_path.name}","urls":{current_count},'
                f'"bytes":{size}}}\n'
            )
            progress_fh.flush()
        current_writer = None
        current_finalize = None
        current_path = None
        batch_index += 1
        current_count = 0

    open_batch()

    if args.backend == "sync":
        url_iter: Iterator[str] = _crawl_sync(args.url, args)
    else:
        url_iter = _crawl_async_streaming(args.url, args)

    last_report = time.monotonic()
    for url in url_iter:
        if args.max_urls and written_total >= args.max_urls:
            log.info(
                "  · stopping: reached --max-urls=%d",
                args.max_urls,
            )
            break
        line = (url + "\n") if args.format == "txt" else (json.dumps({"url": url}) + "\n")
        current_writer(line.encode("utf-8"))
        current_count += 1
        written_total += 1

        # Periodic progress every 30s with running totals.
        now = time.monotonic()
        if now - last_report > 30:
            elapsed = now - started
            log.info(
                "  · progress  %s",
                kv(total=written_total, urls_per_s=round(written_total / elapsed, 1)),
            )
            last_report = now

        if progress_fh and now - last_progress_event > 5:
            progress_fh.write(
                f'{{"event":"urls","total":{written_total}}}\n'
            )
            progress_fh.flush()
            last_progress_event = now

        if current_count >= args.batch_size:
            close_batch()
            open_batch()

    if current_writer is not None:
        close_batch()

    elapsed = time.monotonic() - started
    rate = written_total / elapsed if elapsed > 0 else 0.0
    log.info("✓ done  %s", kv(
        urls=written_total,
        batches=batch_index,
        elapsed_s=round(elapsed, 2),
        urls_per_s=round(rate, 1),
        out=str(args.output),
    ))

    if progress_fh:
        progress_fh.write(
            f'{{"event":"done","urls":{written_total},'
            f'"rate":{rate:.1f}}}\n'
        )
        progress_fh.close()

    # Atomic write of the manifest (write to temp + rename) so a
    # crash mid-write can't leave a half-baked manifest.
    manifest_path = args.output / "manifest.json"
    manifest_tmp = manifest_path.with_suffix(".json.tmp")
    manifest = {
        "usp-max": __version__,
        "url": args.url,
        "urls_total": written_total,
        "batches_written": batch_index,
        "batch_size": args.batch_size,
        "format": args.format,
        "compress": args.compress,
        "tar": args.tar,
        "elapsed_seconds": round(elapsed, 3),
        "rate_urls_per_s": round(rate, 1),
        "backend": args.backend,
        "parser": args.parser,
        "exit_reason": "max_urls_reached" if (args.max_urls and written_total >= args.max_urls) else "natural",
    }
    try:
        with manifest_tmp.open("w") as f:
            json.dump(manifest, f, indent=2)
        manifest_tmp.replace(manifest_path)
        log.info("  manifest: %s", manifest_path)
    except OSError as ex:
        log.warning("could not write manifest %s: %s", manifest_path, ex)