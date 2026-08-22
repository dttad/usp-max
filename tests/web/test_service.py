"""Tests for :class:`usp.web.service.CrawlService`."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import pytest

from usp.web.service import (
    CrawlService,
    CrawlSettings,
    JobStatus,
    _QueueLogHandler,
)

# --- CrawlSettings ----------------------------------------------------


def test_settings_as_dict_strips_none_proxy():
    s = CrawlSettings(url="https://x/")
    d = s.as_dict()
    assert d["url"] == "https://x/"
    assert d["proxy"] is None
    assert d["concurrency"] == 16


def test_settings_as_dict_records_proxy():
    s = CrawlSettings(url="https://x/", proxy="http://127.0.0.1:8080")
    assert s.as_dict()["proxy"] == "http://127.0.0.1:8080"


# --- CrawlService.start() --------------------------------------------


async def test_start_creates_a_job_with_unique_id(output_root: Path):
    svc = CrawlService(output_root=output_root, max_jobs=2)
    job = svc.start(CrawlSettings(url="https://x/"))
    assert job.id
    assert job.status == JobStatus.QUEUED
    assert job.output_dir.is_dir()
    assert svc.get(job.id) is job
    # Drain to completion so the task doesn't leak.
    await asyncio.wait_for(job.task, timeout=2.0)


async def test_start_respects_max_jobs(output_root: Path):
    svc = CrawlService(output_root=output_root, max_jobs=1)
    job = svc.start(CrawlSettings(url="https://x/"))

    # Drain the first "started" event so the worker attaches its handler
    # before the second start() bumps into the limit.
    await asyncio.wait_for(job.events.get(), timeout=2.0)

    with pytest.raises(RuntimeError, match="too many concurrent jobs"):
        svc.start(CrawlSettings(url="https://y/"))

    svc.cancel(job.id)
    try:
        await asyncio.wait_for(job.task, timeout=2.0)
    except asyncio.CancelledError:
        pass


async def test_list_returns_serialisable_dicts(output_root: Path):
    svc = CrawlService(output_root=output_root, max_jobs=2)
    job = svc.start(CrawlSettings(url="https://x/"))
    jobs = svc.list()
    assert len(jobs) == 1
    assert jobs[0]["id"] == job.id
    assert jobs[0]["status"] == "queued"
    svc.cancel(job.id)
    try:
        await asyncio.wait_for(job.task, timeout=2.0)
    except asyncio.CancelledError:
        # ``task.cancel()`` raises CancelledError when awaited; swallow.
        pass


# --- stream() lifecycle -----------------------------------------------


async def test_stream_yields_snapshot_then_error(output_root: Path, monkeypatch):
    """A crawl that fails fast should still produce a snapshot, the
    in-flight events, and a final 'error' sentinel."""
    from usp.fetcher import async_client as _ac

    class _BoomClient:
        async def get(self, url):
            raise ConnectionError("boom")

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", lambda **kw: _BoomClient())

    svc = CrawlService(output_root=output_root, max_jobs=2)
    job = svc.start(CrawlSettings(url="https://x/", concurrency=2))
    await asyncio.wait_for(job.task, timeout=2.0)

    events = []
    async for evt in svc.stream(job.id):
        events.append(evt)
        if evt.get("type") in ("error", "done", "cancelled"):
            break
        if len(events) > 200:
            break

    types = [e["type"] for e in events]
    assert "snapshot" in types
    assert "started" in types
    # All fetches failed → status flipped to ERROR by the service.
    assert "error" in types


async def test_log_handler_emits_messages(output_root: Path, monkeypatch):
    """The QueueLogHandler should surface log records to the SSE queue."""
    from usp.fetcher import async_client as _ac

    class _EmptyClient:
        async def get(self, url):
            from usp.fetcher.async_client import _AsyncResponse

            return _AsyncResponse(url=url, status_code=200, data=b"", headers={})

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", lambda **kw: _EmptyClient())

    svc = CrawlService(output_root=output_root, max_jobs=2)
    job = svc.start(CrawlSettings(url="https://x/sitemap.xml", concurrency=1))

    # Wait for the worker to attach the queue handler before emitting.
    # The worker first emits a "started" event, then attaches the handler.
    # We block on that ordering by reading the "started" event directly.
    started = await asyncio.wait_for(job.events.get(), timeout=2.0)
    assert started["type"] == "started"

    # Now emit a record — the worker has attached its handler.
    logging.getLogger("usp-max.test").warning("hello web %d", 42)

    await asyncio.wait_for(job.task, timeout=2.0)
    seen_logs = []
    async for evt in svc.stream(job.id):
        seen_logs.append(evt)
        if evt.get("type") in ("done", "error", "cancelled"):
            break
        if len(seen_logs) > 200:
            break

    log_events = [e for e in seen_logs if e.get("type") == "log"]
    assert any("hello web 42" in e.get("message", "") for e in log_events), log_events


async def test_homepage_triggers_discovery(output_root: Path, monkeypatch):
    """A homepage URL should be resolved via robots.txt and the
    discovered sitemap URLs should appear in a ``resolved`` SSE event
    before the crawl begins.
    """
    from usp.fetcher import async_client as _ac

    ROBOTS = (
        b"User-Agent: *\n"
        b"Sitemap: https://x.example/sitemap.xml\n"
        b"Sitemap: https://x.example/sitemap-news.xml\n"
    )

    class _FakeClient:
        async def get(self, url):
            from usp.fetcher.async_client import _AsyncResponse

            if url.endswith("/robots.txt"):
                return _AsyncResponse(url=url, status_code=200, data=ROBOTS, headers={})
            if url.endswith(".xml"):
                return _AsyncResponse(
                    url=url,
                    status_code=200,
                    data=(
                        b'<?xml version="1.0"?>'
                        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        b"<url><loc>https://a/</loc></url></urlset>"
                    ),
                    headers={},
                )
            return _AsyncResponse(url=url, status_code=404, data=b"", headers={})

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", lambda **kw: _FakeClient())

    svc = CrawlService(output_root=output_root, max_jobs=2)
    job = svc.start(CrawlSettings(url="https://x.example/"))
    await asyncio.wait_for(job.task, timeout=2.0)

    events = []
    async for evt in svc.stream(job.id):
        events.append(evt)
        if evt.get("type") in ("done", "error", "cancelled"):
            break
        if len(events) > 200:
            break

    resolved = [e for e in events if e.get("type") == "resolved"]
    assert resolved, events
    seeds = resolved[0]["seed_urls"]
    assert "https://x.example/sitemap.xml" in seeds
    assert "https://x.example/sitemap-news.xml" in seeds


def test_handler_is_a_logging_handler():
    """The handler must be a real ``logging.Handler`` subclass."""
    assert issubclass(_QueueLogHandler, logging.Handler)


# --- cancel() ---------------------------------------------------------


async def test_cancel_marks_job_as_cancelled(output_root: Path, monkeypatch):
    from usp.fetcher import async_client as _ac

    # Block forever on .get() until cancelled.
    class _BlockClient:
        async def get(self, url):
            await asyncio.sleep(60)

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", lambda **kw: _BlockClient())

    svc = CrawlService(output_root=output_root, max_jobs=2)
    job = svc.start(CrawlSettings(url="https://x/", concurrency=1))

    # Let it start.
    await asyncio.sleep(0.05)
    ok = svc.cancel(job.id)
    assert ok is True
    try:
        await asyncio.wait_for(job.task, timeout=2.0)
    except asyncio.CancelledError:
        pass
    assert job.status == JobStatus.CANCELLED


# --- history persistence ---------------------------------------------


async def test_history_persists_to_disk(output_root: Path, monkeypatch):
    """After a job finishes, the history file contains the final record."""
    from usp.fetcher import async_client as _ac

    class _EmptyClient:
        async def get(self, url):
            from usp.fetcher.async_client import _AsyncResponse

            return _AsyncResponse(url=url, status_code=200, data=b"", headers={})

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", lambda **kw: _EmptyClient())

    svc = CrawlService(output_root=output_root, max_jobs=2)
    job = svc.start(CrawlSettings(url="https://x/sitemap.xml"))
    await asyncio.wait_for(job.task, timeout=2.0)

    history_path = output_root / "history.jsonl"
    assert history_path.exists()
    lines = [ln for ln in history_path.read_text().splitlines() if ln]
    assert len(lines) >= 1
    record = json.loads(lines[-1])
    assert record["url"] == "https://x/sitemap.xml"
    assert record["status"] in ("done", "error", "cancelled")


async def test_history_is_loaded_on_restart(output_root: Path):
    """A fresh CrawlService reading the same directory picks up past jobs."""

    from usp.fetcher import async_client as _ac

    class _EmptyClient:
        async def get(self, url):
            from usp.fetcher.async_client import _AsyncResponse

            return _AsyncResponse(url=url, status_code=200, data=b"", headers={})

        async def aclose(self):
            return None

    monkey = pytest.MonkeyPatch()
    monkey.setattr(_ac, "AsyncWebClient", lambda **kw: _EmptyClient())
    try:
        svc1 = CrawlService(output_root=output_root, max_jobs=2)
        job1 = svc1.start(CrawlSettings(url="https://alpha.example/sitemap.xml"))
        await asyncio.wait_for(job1.task, timeout=2.0)

        svc2 = CrawlService(output_root=output_root, max_jobs=2)
        ids = [j["id"] for j in svc2.list()]
        assert job1.id in ids
    finally:
        monkey.undo()


async def test_mid_crawl_jobs_marked_as_error_on_restart(output_root: Path):
    """If a previous server crashed mid-crawl, the rehydrated row shows error."""
    import json as _json

    history_path = output_root / "history.jsonl"
    history_path.write_text(
        _json.dumps(
            {
                "id": "orphan1234567",
                "url": "https://crashed.example/",
                "created_at": time.time(),
                "started_at": time.time(),
                "status": "running",
                "settings": {"url": "https://crashed.example/", "concurrency": 1},
                "output_dir": str(output_root / "orphan1234567"),
            }
        )
        + "\n"
    )
    svc = CrawlService(output_root=output_root, max_jobs=2)
    rows = svc.list()
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert "restarted" in (rows[0]["error"] or "")


async def test_list_filters_by_search_substring(output_root: Path, monkeypatch):
    """`list(search=…)` matches URL substrings case-insensitively."""
    from usp.fetcher import async_client as _ac

    class _EmptyClient:
        async def get(self, url):
            from usp.fetcher.async_client import _AsyncResponse

            return _AsyncResponse(url=url, status_code=200, data=b"", headers={})

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", lambda **kw: _EmptyClient())
    svc = CrawlService(output_root=output_root, max_jobs=4)
    svc.start(CrawlSettings(url="https://example.com/sitemap.xml"))
    svc.start(CrawlSettings(url="https://other.org/sitemap.xml"))
    # Let both jobs finish so they land in history.
    for j in list(svc._jobs.values()):
        await asyncio.wait_for(j.task, timeout=2.0)

    rows = svc.list(search="example")
    assert len(rows) == 1
    assert "example" in rows[0]["url"]


async def test_list_respects_limit(output_root: Path, monkeypatch):
    from usp.fetcher import async_client as _ac

    class _EmptyClient:
        async def get(self, url):
            from usp.fetcher.async_client import _AsyncResponse

            return _AsyncResponse(url=url, status_code=200, data=b"", headers={})

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", lambda **kw: _EmptyClient())
    svc = CrawlService(output_root=output_root, max_jobs=8)
    for i in range(5):
        svc.start(CrawlSettings(url=f"https://x{i}.example/"))
    for j in list(svc._jobs.values()):
        await asyncio.wait_for(j.task, timeout=2.0)
    rows = svc.list(limit=2)
    assert len(rows) == 2
