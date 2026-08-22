"""Tests for :mod:`usp.fetcher.discovery`."""

from __future__ import annotations

from usp.fetcher.discovery import (
    discover_sitemap_urls,
    looks_like_sitemap_url,
)

# --- looks_like_sitemap_url -------------------------------------------


def test_looks_like_sitemap_recognises_xml_extension():
    assert looks_like_sitemap_url("https://x.example/sitemap.xml")
    assert looks_like_sitemap_url("https://x.example/SITEMAP.XML")


def test_looks_like_sitemap_recognises_path_segment():
    assert looks_like_sitemap_url("https://x.example/sitemap/index.xml")
    assert looks_like_sitemap_url("https://x.example/store/sitemaps/x.xml")


def test_looks_like_sitemap_rejects_plain_homepage():
    assert not looks_like_sitemap_url("https://x.example/")
    assert not looks_like_sitemap_url("https://x.example/about")
    assert not looks_like_sitemap_url("https://x.example/store/games")


def test_looks_like_sitemap_ignores_query_string():
    assert looks_like_sitemap_url("https://x.example/sitemap.xml?ver=1")
    assert looks_like_sitemap_url("https://x.example/sitemap/index.xml#top")


# --- discover_sitemap_urls --------------------------------------------


async def test_discovers_via_robots_txt():
    robots = (
        b"User-Agent: *\n"
        b"Disallow: /admin\n"
        b"Sitemap: https://x.example/sitemap.xml\n"
        b"  Sitemap:   https://x.example/sitemap-news.xml  \n"
    )

    async def fetch(url):
        return 200, robots, url

    found = await discover_sitemap_urls("https://x.example/", fetch)
    assert found == [
        "https://x.example/sitemap.xml",
        "https://x.example/sitemap-news.xml",
    ]


async def test_falls_back_to_known_paths_when_no_robots():
    async def fetch(url):
        if url.endswith("/robots.txt"):
            return 404, b"", url
        if url.endswith("/sitemap.xml"):
            return 200, b'<?xml version="1.0"?><urlset/>', url
        return 404, b"", url

    found = await discover_sitemap_urls("https://x.example/", fetch)
    assert found
    assert found[0].endswith("/sitemap.xml")


async def test_no_results_when_nothing_matches():
    async def fetch(url):
        return 404, b"", url

    found = await discover_sitemap_urls("https://x.example/", fetch)
    assert found == []


async def test_bad_homepage_url_returns_empty():
    async def fetch(url):
        raise AssertionError("should not be called")

    assert await discover_sitemap_urls("not-a-url", fetch) == []


async def test_fetch_failure_is_swallowed():
    """A transport error must not abort discovery."""

    async def fetch(url):
        if url.endswith("/robots.txt"):
            raise ConnectionError("boom")
        return 200, b'<?xml version="1.0"?><urlset/>', url

    found = await discover_sitemap_urls("https://x.example/", fetch)
    # robots.txt raised → fell through to known paths probe.
    assert any(u.endswith("/sitemap.xml") for u in found)


async def test_duplicate_sitemap_urls_are_deduplicated():
    robots = (
        b"Sitemap: https://x.example/sitemap.xml\n"
        b"Sitemap: https://x.example/sitemap.xml\n"  # dup
    )

    async def fetch(url):
        return 200, robots, url

    found = await discover_sitemap_urls("https://x.example/", fetch)
    assert found == ["https://x.example/sitemap.xml"]


async def test_does_not_use_html_response_as_sitemap():
    """A 200 HTML response should not be accepted by the known-path probe."""

    async def fetch(url):
        if url.endswith("/robots.txt"):
            return 404, b"", url
        if url.endswith("/sitemap.xml"):
            return 200, b"<!doctype html><html><body>hi</body></html>", url
        return 404, b"", url

    found = await discover_sitemap_urls("https://x.example/", fetch)
    # The known-path probe rejected the HTML response.
    assert not any(u.endswith("/sitemap.xml") for u in found)
