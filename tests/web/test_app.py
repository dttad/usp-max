"""Tests for the Starlette HTTP routes (no actual network)."""

from __future__ import annotations


def test_health_endpoint(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body


def test_create_crawl_validates_url(client):
    """Missing URL is rejected with 400."""
    r = client.post("/api/crawl", json={})
    assert r.status_code == 400
    assert "url" in r.json()["detail"]


def test_create_crawl_rejects_non_http(client):
    r = client.post("/api/crawl", json={"url": "ftp://example.com/"})
    assert r.status_code == 400


def test_create_crawl_rejects_invalid_proxy_int_concurrency(client):
    """Non-integer concurrency is rejected."""
    r = client.post(
        "/api/crawl",
        json={"url": "https://x.example/", "concurrency": "many"},
    )
    assert r.status_code == 400


def test_create_crawl_rejects_out_of_range(client):
    r = client.post(
        "/api/crawl",
        json={"url": "https://x.example/", "fanout_cap": 999_999},
    )
    assert r.status_code == 400


def test_create_crawl_rejects_unknown_parser(client):
    r = client.post(
        "/api/crawl",
        json={"url": "https://x.example/", "parser": "magic"},
    )
    assert r.status_code == 400


def test_create_crawl_accepts_minimal_payload(client, monkeypatch):
    """A minimal valid payload should kick off a crawl job."""

    # Patch the AsyncWebClient so no network is touched.
    from usp.fetcher import async_client as _ac

    class _FakeClient:
        async def get(self, url):
            from usp.fetcher.async_client import _AsyncResponse

            return _AsyncResponse(url=url, status_code=200, data=b"", headers={})

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", lambda **kw: _FakeClient())

    r = client.post("/api/crawl", json={"url": "https://example.com/"})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "queued"
    assert body["settings"]["url"] == "https://example.com/"
    assert body["settings"]["concurrency"] == 16  # default
    assert body["settings"]["parser"] == "auto"


def test_create_crawl_accepts_proxy(client, monkeypatch):
    """Proxy field is captured into settings and forwarded downstream."""
    from usp.fetcher import async_client as _ac

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

        async def get(self, url):
            from usp.fetcher.async_client import _AsyncResponse

            return _AsyncResponse(url=url, status_code=200, data=b"", headers={})

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", _FakeClient)

    r = client.post(
        "/api/crawl",
        json={
            "url": "https://example.com/",
            "proxy": "http://127.0.0.1:8080",
            "concurrency": 4,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["settings"]["proxy"] == "http://127.0.0.1:8080"


def test_list_jobs_includes_created_job(client, monkeypatch):
    from usp.fetcher import async_client as _ac

    class _FakeClient:
        async def get(self, url):
            from usp.fetcher.async_client import _AsyncResponse

            return _AsyncResponse(url=url, status_code=200, data=b"", headers={})

        async def aclose(self):
            return None

    monkeypatch.setattr(_ac, "AsyncWebClient", lambda **kw: _FakeClient())

    client.post("/api/crawl", json={"url": "https://a.example/"})
    client.post("/api/crawl", json={"url": "https://b.example/"})

    r = client.get("/api/jobs")
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert len(jobs) == 2
    # Most recent first.
    assert jobs[0]["created_at"] >= jobs[1]["created_at"]


def test_get_unknown_job_returns_404(client):
    r = client.get("/api/jobs/does-not-exist")
    assert r.status_code == 404


def test_cancel_unknown_job_returns_404(client):
    r = client.delete("/api/jobs/does-not-exist")
    assert r.status_code == 404


def test_index_serves_fallback_when_no_static(client):
    """Without a built frontend, ``/`` returns the build instructions."""
    r = client.get("/")
    # Either it serves the dist/index.html or the fallback page. Either
    # way it must be 200 with text/html.
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
