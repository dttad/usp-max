"""Helpers used by bench/profile.py.

Lives in its own module so the CLI can subprocess into it without
importing CLI-side code.
"""
from __future__ import annotations

from urllib.parse import urlparse


def make_reroute_client(base: str):
    """Wrap RequestsWebClient so all GETs are routed to ``base + path``."""
    from usp.web_client.requests_client import RequestsWebClient

    delegate = RequestsWebClient()

    class Reroute:
        def get(self, url):
            p = urlparse(url)
            new = base.rstrip("/") + p.path
            return delegate.get(new)

        def set_max_response_data_length(self, n):
            pass

        def set_timeout(self, t):
            pass

        def set_proxies(self, p):
            pass

    return Reroute()


def crawl(homepage: str, web_client, cap: int) -> list[str]:
    from usp.tree import sitemap_tree_for_homepage

    def cap_leaves(urls, level, parents):
        return sorted(urls)[:cap]

    tree = sitemap_tree_for_homepage(
        homepage,
        web_client=web_client,
        use_robots=True,
        use_known_paths=False,
        recurse_list_callback=cap_leaves,
        normalize_homepage_url=False,
    )
    pages = []
    for page in tree.all_pages():
        pages.append(page.url)
    return pages
