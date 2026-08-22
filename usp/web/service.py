"""Crawl job registry + log streaming for the web UI.

A *job* is one in-flight crawl. Each job owns:

* an :class:`asyncio.Queue` of ``Event`` items (log lines, URL hits,
  progress updates, lifecycle markers);
* a status enum (``queued`` / ``running`` / ``done`` / ``error`` /
  ``cancelled``);
* counters (URLs seen, sitemaps fetched / failed, elapsed seconds);
* a settings snapshot (proxy, concurrency, fanout-cap, …) captured at
  submission time.

The :class:`CrawlService` keeps all live jobs in a dict keyed by job
id and exposes start/list/get/stream/cancel operations.

Log capture is implemented with a :class:`logging.Handler` subclass that
formats each record using the existing :mod:`usp.cli._log` formatter
and pushes it onto the queue. This means the web UI shows the exact
same coloured logs that the CLI shows on a TTY — minus the ANSI
escapes, which we strip.
"""

from __future__ import annotations

import asyncio
import enum
import gzip
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from usp import __version__

log = logging.getLogger("usp-max.web")


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class CrawlSettings:
    """Snapshot of user-supplied settings for a job."""

    url: str
    concurrency: int = 16
    fanout_cap: int = 200
    max_depth: int = 16
    parser: str = "auto"  # "auto" | "expat"
    proxy: str | None = None
    user_agent: str = field(
        default_factory=lambda: (
            f"usp-max/{__version__} (+https://github.com/dttad/usp-max)"
        )
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "concurrency": self.concurrency,
            "fanout_cap": self.fanout_cap,
            "max_depth": self.max_depth,
            "parser": self.parser,
            "proxy": self.proxy,
            "user_agent": self.user_agent,
        }


@dataclass
class JobStats:
    """Counters tracked for a running job."""

    started_at: float = 0.0
    ended_at: float = 0.0
    urls_total: int = 0
    sitemaps_fetched: int = 0
    sitemaps_failed: int = 0
    bytes_written: int = 0

    def as_dict(self) -> dict[str, Any]:
        elapsed = (
            (self.ended_at or time.monotonic()) - self.started_at
            if self.started_at
            else 0.0
        )
        rate = self.urls_total / elapsed if elapsed > 0 else 0.0
        return {
            "elapsed_s": round(elapsed, 2),
            "urls_total": self.urls_total,
            "sitemaps_fetched": self.sitemaps_fetched,
            "sitemaps_failed": self.sitemaps_failed,
            "bytes_written": self.bytes_written,
            "urls_per_s": round(rate, 1),
        }


# ---------------------------------------------------------------------------
# Job container
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    settings: CrawlSettings
    output_dir: Path
    created_at: float
    status: JobStatus = JobStatus.QUEUED
    stats: JobStats = field(default_factory=JobStats)
    error: str | None = None
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    cancel_requested: bool = False

    def event(self, kind: str, **payload: Any) -> dict[str, Any]:
        """Build a serialisable event dict."""
        return {"type": kind, "ts": time.time(), **payload}

    def to_history_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot for the history file/API."""
        return {
            "id": self.id,
            "status": self.status.value,
            "url": self.settings.url,
            "created_at": self.created_at,
            "started_at": self.stats.started_at,
            "ended_at": self.stats.ended_at,
            "urls_total": self.stats.urls_total,
            "sitemaps_fetched": self.stats.sitemaps_fetched,
            "sitemaps_failed": self.stats.sitemaps_failed,
            "bytes_written": self.stats.bytes_written,
            "error": self.error,
            "output_dir": str(self.output_dir),
            "settings": self.settings.as_dict(),
        }


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


# ---------------------------------------------------------------------------
# Logging bridge
# ---------------------------------------------------------------------------


class _QueueLogHandler(logging.Handler):
    """Push formatted log records onto a queue for the SSE stream."""

    def __init__(self, job: Job) -> None:
        super().__init__(level=logging.DEBUG)
        self._job = job

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._job.events.put_nowait(
                self._job.event("log", level=record.levelname, message=msg)
            )
        except Exception:  # noqa: BLE001
            self.handleError(record)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CrawlService:
    """In-memory registry of crawl jobs running on the same event loop.

    Job metadata is persisted to ``<output_root>/history.jsonl`` so the
    history survives a server restart. Live ``events`` queues are not
    persisted — only the on-disk urls.txt + manifest.json are kept.
    """

    HISTORY_FILENAME = "history.jsonl"

    def __init__(
        self,
        *,
        output_root: Path | None = None,
        max_jobs: int = 8,
        max_log_lines: int = 5000,
        url_path_template: str = "urls-{index:05d}.txt",
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._output_root = Path(output_root) if output_root else Path("./usp-web-out")
        self._max_jobs = max(1, max_jobs)
        self._max_log_lines = max(100, max_log_lines)
        self._url_path_template = url_path_template
        self._output_root.mkdir(parents=True, exist_ok=True)
        self._history_path = self._output_root / self.HISTORY_FILENAME
        # Stash live tasks by id so we can clean up properly on restart.
        self._active_ids: set[str] = set()
        # Eager-load history so the UI is populated on first paint.
        self._load_history()

    # ----- history persistence -----------------------------------------

    def _load_history(self) -> None:
        """Re-populate ``self._jobs`` from ``history.jsonl``.

        We mark every loaded job as RUNNING if the previous server
        crashed mid-crawl; the UI shows it as 'error' since the worker
        is no longer alive. Live jobs never reappear from history
        because the JSONL write happens at job start and again at
        finish — only the final write reflects the terminal state.
        """
        if not self._history_path.exists():
            return
        try:
            with self._history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    job = self._rehydrate(rec)
                    if job is not None:
                        self._jobs[job.id] = job
        except OSError as ex:
            log.warning("could not read history %s: %s", self._history_path, ex)

    def _rehydrate(self, rec: dict[str, Any]) -> Job | None:
        """Build a Job from a history record (no live state)."""
        try:
            settings = CrawlSettings(
                url=rec["url"],
                concurrency=rec.get("settings", {}).get("concurrency", 16),
                fanout_cap=rec.get("settings", {}).get("fanout_cap", 200),
                max_depth=rec.get("settings", {}).get("max_depth", 16),
                parser=rec.get("settings", {}).get("parser", "auto"),
                proxy=rec.get("settings", {}).get("proxy"),
                user_agent=rec.get("settings", {}).get(
                    "user_agent",
                    f"usp-max/{__version__} (+https://github.com/dttad/usp-max)",
                ),
            )
            stats = JobStats(
                started_at=rec.get("started_at", 0.0),
                ended_at=rec.get("ended_at", 0.0),
                urls_total=rec.get("urls_total", 0),
                sitemaps_fetched=rec.get("sitemaps_fetched", 0),
                sitemaps_failed=rec.get("sitemaps_failed", 0),
                bytes_written=rec.get("bytes_written", 0),
            )
            # If the previous server died mid-crawl, mark the job as
            # error so the UI doesn't show a permanently-running row.
            raw_status = rec.get("status", JobStatus.DONE.value)
            if raw_status in (JobStatus.RUNNING.value, JobStatus.QUEUED.value):
                raw_status = JobStatus.ERROR.value
                if not rec.get("error"):
                    rec["error"] = "server restarted while this crawl was running"
            job = Job(
                id=rec["id"],
                settings=settings,
                output_dir=Path(rec.get("output_dir", self._output_root / rec["id"])),
                created_at=rec.get("created_at", time.time()),
                status=JobStatus(raw_status),
                stats=stats,
                error=rec.get("error"),
            )
            return job
        except (KeyError, ValueError) as ex:
            log.warning("skipped malformed history record: %s", ex)
            return None

    def _persist(self, job: Job) -> None:
        """Append one history record to disk. Best-effort; logs on failure."""
        try:
            with self._history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(job.to_history_dict()) + "\n")
        except OSError as ex:
            log.warning("could not persist history %s: %s", self._history_path, ex)

    def _rewrite_history(self) -> None:
        """Re-write the full history file from the in-memory jobs.

        Used at job finish so the final record (with terminal status +
        stats) replaces the placeholder written at start.
        """
        try:
            tmp_path = self._history_path.with_suffix(".jsonl.tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                for job in self._jobs.values():
                    f.write(json.dumps(job.to_history_dict()) + "\n")
            tmp_path.replace(self._history_path)
        except OSError as ex:
            log.warning("could not rewrite history %s: %s", self._history_path, ex)

    # ----- accessors -----------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(
        self, *, search: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return jobs (most-recent first), optionally filtered by URL substring."""
        needle = (search or "").strip().lower()
        out: list[dict[str, Any]] = []
        for j in self._jobs.values():
            if needle and needle not in j.settings.url.lower():
                continue
            elapsed = 0.0
            if j.stats.started_at:
                elapsed = (j.stats.ended_at or time.monotonic()) - j.stats.started_at
            out.append(
                {
                    "id": j.id,
                    "status": j.status.value,
                    "url": j.settings.url,
                    "created_at": j.created_at,
                    "started_at": j.stats.started_at,
                    "ended_at": j.stats.ended_at,
                    "urls_total": j.stats.urls_total,
                    "sitemaps_fetched": j.stats.sitemaps_fetched,
                    "sitemaps_failed": j.stats.sitemaps_failed,
                    "bytes_written": j.stats.bytes_written,
                    "elapsed_s": round(elapsed, 2),
                    "error": j.error,
                }
            )
        out.sort(key=lambda r: r["created_at"], reverse=True)
        if limit is not None and limit > 0:
            out = out[:limit]
        return out

    # ----- lifecycle -----------------------------------------------------

    def start(self, settings: CrawlSettings) -> Job:
        active = sum(
            1
            for j in self._jobs.values()
            if j.status in (JobStatus.RUNNING, JobStatus.QUEUED)
        )
        if active >= self._max_jobs:
            raise RuntimeError(
                f"too many concurrent jobs (limit={self._max_jobs}); "
                f"cancel or wait for an existing job to finish."
            )
        job_id = uuid.uuid4().hex[:12]
        out_dir = self._output_root / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        job = Job(
            id=job_id,
            settings=settings,
            output_dir=out_dir,
            created_at=time.time(),
        )
        job.task = asyncio.create_task(self._run(job), name=f"crawl-{job_id}")
        self._jobs[job_id] = job
        self._active_ids.add(job_id)
        # Best-effort: write a placeholder record so the row shows up
        # immediately. The final state is rewritten in ``_run``'s finally.
        self._persist(job)
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.task is None:
            return False
        if job.task.done():
            return False
        job.cancel_requested = True
        job.task.cancel()
        return True

    # ----- SSE stream ----------------------------------------------------

    async def stream(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield events for ``job_id`` as they arrive. Ends with a sentinel
        ``event: done`` after the job has finished.
        """
        job = self._jobs.get(job_id)
        if job is None:
            yield {"type": "error", "message": f"unknown job {job_id}"}
            return

        # Replay the initial state so a reconnected client gets a snapshot.
        yield job.event(
            "snapshot",
            id=job.id,
            status=job.status.value,
            settings=job.settings.as_dict(),
            stats=job.stats.as_dict(),
        )

        # Backpressure: cap the buffer so a chatty crawler doesn't OOM us.
        while True:
            try:
                evt = await asyncio.wait_for(job.events.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield job.event("heartbeat", stats=job.stats.as_dict())
                continue

            yield evt
            if evt.get("type") in ("done", "error", "cancelled"):
                return

    # ----- the worker ----------------------------------------------------

    async def _run(self, job: Job) -> None:
        # We do the heavy imports inside the worker so the service module
        # stays cheap to import (and so test runs that don't go to network
        # don't need httpx/lxml loaded at module-import time).
        from usp.fetcher.async_client import AsyncWebClient
        from usp.fetcher.crawler import AsyncCrawler
        from usp.fetcher.discovery import (
            discover_sitemap_urls,
            looks_like_sitemap_url,
        )

        cap = job.settings.fanout_cap

        def cap_fn(urls: Sequence[str], level: int, parents: set[str]):
            return sorted(urls)[:cap]

        # 1) announce.
        await job.events.put(
            job.event(
                "started",
                settings=job.settings.as_dict(),
                output_dir=str(job.output_dir),
            )
        )

        # 2) attach a queue-based log handler so the UI sees live logs.
        handler = _QueueLogHandler(job)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        prev_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)

        job.status = JobStatus.RUNNING
        job.stats.started_at = time.monotonic()

        urls_path = job.output_dir / "urls.txt"
        urls_path_gz = job.output_dir / "urls.txt.gz"
        urls_fh = urls_path.open("w", encoding="utf-8", buffering=1)
        gz_fh = gzip.open(urls_path_gz, "wt", encoding="utf-8")

        crawler: AsyncCrawler | None = None
        client: AsyncWebClient | None = None
        try:
            client = AsyncWebClient(
                http2=True,
                max_connections=max(job.settings.concurrency * 2, 20),
                user_agent=job.settings.user_agent,
                proxy=job.settings.proxy,
            )
            crawler = AsyncCrawler(
                client,
                concurrency=job.settings.concurrency,
                max_depth=job.settings.max_depth,
                recurse_list_callback=cap_fn,
                use_lxml=job.settings.parser != "expat",
                use_rust=job.settings.parser != "expat",
            )

            # Resolve homepage → actual sitemap URLs via robots.txt /
            # known paths. ``crawl_streaming`` expects sitemap URLs;
            # without this step, ``crawl https://play.google.com/`` would
            # try to parse the HTML homepage and fail.
            seed_urls = [job.settings.url]
            if not looks_like_sitemap_url(job.settings.url):
                log.info("→ discovering sitemaps for %s", job.settings.url)

                async def _fetch(u: str):
                    r = await client.get(u)
                    if r.error is not None:
                        return 0, b"", u
                    return r.status_code(), r.raw_data(), r.url()

                discovered = await discover_sitemap_urls(job.settings.url, _fetch)
                if discovered:
                    seed_urls = discovered
                    log.info(
                        "  ✓ discovered %d sitemap(s): %s",
                        len(discovered),
                        ", ".join(discovered[:3])
                        + ("…" if len(discovered) > 3 else ""),
                    )
                else:
                    log.warning("  ✗ no sitemaps discovered; will try the URL as-is")

            await job.events.put(
                job.event(
                    "resolved",
                    seed_urls=seed_urls,
                    homepage=job.settings.url,
                )
            )

            async for url in crawler.crawl_streaming(seed_urls):
                if job.cancel_requested:
                    break
                urls_fh.write(url + "\n")
                gz_fh.write(url + "\n")
                job.stats.urls_total += 1
                job.stats.bytes_written = urls_path.stat().st_size
                # Forward URL hits as events so the UI can show them live.
                # We batch every 50th to avoid drowning the SSE stream.
                if job.stats.urls_total % 50 == 0:
                    await job.events.put(
                        job.event("progress", url=url, **job.stats.as_dict())
                    )

            job.status = JobStatus.CANCELLED if job.cancel_requested else JobStatus.DONE
            # If the crawler never managed to fetch a single sitemap, treat
            # the run as a failure even though the iterator finished cleanly.
            if (
                job.status == JobStatus.DONE
                and crawler is not None
                and crawler.sitemaps_fetched == 0
                and crawler.sitemaps_failed > 0
            ):
                job.status = JobStatus.ERROR
                if not job.error:
                    job.error = (
                        f"all {crawler.sitemaps_failed} sitemap fetch(es) failed"
                    )
        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            log.warning("crawl %s cancelled", job.id)
        except Exception as ex:  # noqa: BLE001
            job.status = JobStatus.ERROR
            job.error = str(ex)
            log.exception("crawl %s failed", job.id)
        finally:
            job.stats.ended_at = time.monotonic()
            urls_fh.close()
            gz_fh.close()
            if client is not None:
                await client.aclose()
            if crawler is not None:
                job.stats.sitemaps_fetched = crawler.sitemaps_fetched
                job.stats.sitemaps_failed = crawler.sitemaps_failed

            # Final manifest.
            manifest = {
                "usp-max": __version__,
                "job_id": job.id,
                "url": job.settings.url,
                "status": job.status.value,
                "error": job.error,
                "stats": job.stats.as_dict(),
                "settings": job.settings.as_dict(),
            }
            (job.output_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True)
            )

            root.removeHandler(handler)
            root.setLevel(prev_level)

            # Persist the terminal state. We rewrite the whole history
            # file rather than appending a duplicate so the final
            # record supersedes the placeholder written at start.
            self._active_ids.discard(job.id)
            self._rewrite_history()

            await job.events.put(
                job.event(
                    job.status.value,
                    **job.stats.as_dict(),
                    error=job.error,
                )
            )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_service: CrawlService | None = None


def get_service(output_root: Path | None = None) -> CrawlService:
    """Return the process-wide :class:`CrawlService` singleton.

    ``output_root`` is honoured on the first call only.
    """
    global _service
    if _service is None:
        _service = CrawlService(output_root=output_root)
    return _service


def reset_service() -> None:
    """Discard the process-wide singleton. Test-only helper."""
    global _service
    _service = None
