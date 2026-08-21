"""lxml-based parser for Phase 7a.

Drops in for ``XMLSitemapParser`` when ``USP_USE_LXML=1``. Streams the
XML via ``lxml.etree.iterparse`` and clears elements after processing
to keep memory bounded.
"""
from __future__ import annotations

import io
import logging
import re
from decimal import Decimal
from typing import Iterator

from lxml import etree

from usp.exceptions import SitemapXMLParsingException
from usp.helpers import (
    html_unescape_strip,
    parse_iso8601_date,
    parse_rfc2822_date,
    ungzipped_response_content,
)
from usp.objects.page import (
    SITEMAP_PAGE_DEFAULT_PRIORITY,
    SitemapImage,
    SitemapNewsStory,
    SitemapPage,
    SitemapPageChangeFrequency,
)
from usp.objects.sitemap import (
    IndexRobotsTxtSitemap,
    IndexXMLSitemap,
    InvalidSitemap,
    PagesAtomSitemap,
    PagesRSSSitemap,
    PagesTextSitemap,
    PagesXMLSitemap,
)

log = logging.getLogger(__name__)

_NS_RE = re.compile(r"\{[^}]+\}")


def _localname(tag: str) -> str:
    """Strip namespace URI from a Clark-notation tag."""
    m = _NS_RE.match(tag)
    return tag[m.end():] if m else tag


def _child(elem, name: str):
    for child in elem:
        if _localname(child.tag) == name:
            return child
    return None


def _children(elem, name: str) -> Iterator:
    for child in elem:
        if _localname(child.tag) == name:
            yield child


def _text(elem) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


class _PageBuilder:
    """Accumulates per-<url> data; produces a SitemapPage when the URL closes."""

    __slots__ = (
        "url", "last_modified", "priority", "change_frequency",
        "images", "news", "_in_news",
    )

    def __init__(self):
        self.url: str | None = None
        self.last_modified = None
        self.priority = SITEMAP_PAGE_DEFAULT_PRIORITY
        self.change_frequency = SitemapPageChangeFrequency.DAILY
        self.images: list[SitemapImage] = []
        self.news: SitemapNewsStory | None = None
        self._in_news = False

    def add_element(self, elem) -> None:
        """Process an <end> event for a child of <url>."""
        local = _localname(elem.tag)
        text = _text(elem)
        if local == "loc" and not self._in_news:
            self.url = html_unescape_strip(text) or None
        elif local == "lastmod":
            try:
                self.last_modified = parse_iso8601_date(text)
            except Exception:
                pass
        elif local == "priority":
            try:
                p = Decimal(text)
                if 0.0 <= p <= 1.0:
                    self.priority = p
            except Exception:
                pass
        elif local == "changefreq":
            try:
                self.change_frequency = SitemapPageChangeFrequency(text.lower())
            except Exception:
                pass
        elif local == "image":
            for img in elem:
                if _localname(img.tag) == "image" or True:
                    loc_elem = _child(img, "loc")
                    if loc_elem is not None and _text(loc_elem):
                        self.images.append(SitemapImage(
                            loc=html_unescape_strip(_text(loc_elem)),
                            caption=html_unescape_strip(_text(_child(img, "caption"))) or None,
                            title=html_unescape_strip(_text(_child(img, "title"))) or None,
                        ))
                    break
        elif local == "news":
            self._in_news = True
            title = html_unescape_strip(_text(_child(elem, "title"))) or None
            pub_date_text = _text(_child(elem, "publication_date"))
            pub_date = None
            if pub_date_text:
                try:
                    pub_date = parse_rfc2822_date(pub_date_text)
                except Exception:
                    try:
                        pub_date = parse_iso8601_date(pub_date_text)
                    except Exception:
                        pass
            self.news = SitemapNewsStory(title=title, publish_date=pub_date)
        elif local == "title" and self._in_news and self.news:
            self.news = SitemapNewsStory(
                title=html_unescape_strip(text) or self.news.title,
                publish_date=self.news.publish_date,
            )
        elif local == "publication_date" and self._in_news and self.news:
            try:
                self.news = SitemapNewsStory(
                    title=self.news.title,
                    publish_date=parse_rfc2822_date(text),
                )
            except Exception:
                try:
                    self.news = SitemapNewsStory(
                        title=self.news.title,
                        publish_date=parse_iso8601_date(text),
                    )
                except Exception:
                    pass

    def close(self) -> SitemapPage | None:
        if not self.url:
            return None
        return SitemapPage(
            url=self.url,
            last_modified=self.last_modified,
            priority=self.priority,
            change_frequency=self.change_frequency,
            news_story=self.news,
            images=self.images or None,
        )


def _iterparse_bytes(content: bytes, content_type: str | None = None):
    """Generator yielding ``(event, localname, element)`` for each <end> event.

    Handles gunzip automatically.
    """
    return etree.iterparse(_stream_for(content), events=("end",), huge_tree=True)


def _stream_for(content: bytes) -> io.BytesIO:
    """Decode gzip if needed and return a BytesIO ready for iterparse."""
    import gzip
    if content[:2] == b"\x1f\x8b":
        try:
            text = gzip.decompress(content)
        except Exception:
            text = content
    else:
        text = content
    return io.BytesIO(text)


def parse_lxml_pages(content: bytes, url: str) -> list[SitemapPage]:
    """Parse a pages sitemap (urlset) using lxml.iterparse.

    We use both start and end events so we can attach child data to the
    right builder before the closing ``</url>`` is seen.
    """
    pages: list[SitemapPage] = []
    current: _PageBuilder | None = None
    PAGE_TAGS = ("loc", "lastmod", "priority", "changefreq",
                 "image", "news", "title", "publication_date")
    try:
        for event, elem in etree.iterparse(
            _stream_for(content), events=("start", "end"), huge_tree=True
        ):
            local = _localname(elem.tag)
            if event == "start":
                if local == "url" and current is None:
                    current = _PageBuilder()
            else:  # "end"
                if local == "url":
                    if current is not None:
                        page = current.close()
                        if page is not None:
                            pages.append(page)
                        current = None
                    elem.clear()
                elif local in PAGE_TAGS:
                    if current is not None:
                        current.add_element(elem)
                        elem.clear()
                else:
                    elem.clear()
    except etree.XMLSyntaxError as ex:
        log.warning("lxml parse failed for %s: %s", url, ex)
        raise SitemapXMLParsingException(str(ex)) from ex
    return pages


def parse_lxml_index(content: bytes, url: str) -> list[str]:
    """Parse a sitemap-index (sitemapindex) using lxml.iterparse."""
    urls: list[str] = []
    seen: set[str] = set()
    try:
        for _event, elem in _iterparse_bytes(content):
            local = _localname(elem.tag)
            if local == "sitemap":
                loc = _child(elem, "loc")
                if loc is not None:
                    u = html_unescape_strip(_text(loc))
                    if u and u not in seen:
                        seen.add(u)
                        urls.append(u)
                elem.clear()
            elif local == "loc":
                # sitemap-level <loc> (already handled by parent iteration but
                # safe to capture if it appears at top level)
                u = html_unescape_strip(_text(elem))
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
                elem.clear()
            else:
                elem.clear()
    except etree.XMLSyntaxError as ex:
        log.warning("lxml parse failed for %s: %s", url, ex)
        raise SitemapXMLParsingException(str(ex)) from ex
    return urls


def parse_lxml_root(content: bytes, url: str) -> PagesXMLSitemap | IndexXMLSitemap | None:
    """Dispatch on root element: urlset -> pages, sitemapindex -> index."""
    import gzip
    raw = content
    if content[:2] == b"\x1f\x8b":
        try:
            text = gzip.decompress(content)
        except Exception:
            text = content
    else:
        text = content
    # Look at the first non-whitespace to decide
    stripped = text.lstrip()
    if stripped.startswith(b"<urlset") or stripped.startswith(b"<urlset"):
        pages = parse_lxml_pages(content, url)
        return PagesXMLSitemap(url=url, pages=pages)
    if stripped.startswith(b"<sitemapindex"):
        urls = parse_lxml_index(content, url)
        return IndexXMLSitemap(url=url, sub_sitemaps=[])
    return None


def sitemap_root_kind(content: bytes) -> str | None:
    """Cheap sniff of root element name (urlset / sitemapindex / robots.txt)."""
    import gzip
    if content[:2] == b"\x1f\x8b":
        try:
            text = gzip.decompress(content)
        except Exception:
            text = content
    else:
        text = content
    stripped = text.lstrip()[:200]
    if stripped.startswith(b"<urlset"):
        return "pages"
    if stripped.startswith(b"<sitemapindex"):
        return "index"
    if stripped.startswith(b"<rss") or stripped.startswith(b"<feed"):
        return "feed"
    return None
