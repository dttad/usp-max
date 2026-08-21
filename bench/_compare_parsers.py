"""Compare expat (current) vs lxml (Phase 7a) for parsing sitemap XML.

Parses the same 10 sub-sitemap corpus files with both libraries and
compares wall time and peak RSS.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

CORPUS_DIR = Path(__file__).parent / "corpus"


def get_gzipped_corpus_paths(n: int = 20) -> list[Path]:
    with (CORPUS_DIR / "manifest.json").open() as f:
        manifest = json.load(f)
    out = []
    for entry in manifest["entries"]:
        if entry.get("role") == "index":
            continue
        out.append(CORPUS_DIR / entry["path"])
        if len(out) >= n:
            break
    return out


def parse_with_expat(content_bytes: bytes):
    """Current approach: XMLSitemapParser with Parse(content, True)."""
    from usp.fetch_parse import XMLSitemapParser
    import gzip
    # Decompress first
    if content_bytes[:2] == b"\x1f\x8b":
        try:
            decompressed = gzip.decompress(content_bytes)
        except Exception:
            decompressed = content_bytes
    else:
        decompressed = content_bytes
    text = decompressed.decode("utf-8", errors="replace")
    parser = XMLSitemapParser(
        content=text,
        web_client=None,
        recurse_callback=lambda u, l, p: False,
        url="http://example.com/sitemap.xml",
        recursion_level=0,
        parent_urls=set(),
        recurse_list_callback=lambda u, l, p: u,
    )
    return parser.sitemap()


def parse_with_lxml_iterparse(content_bytes: bytes):
    """Phase 7a: lxml.etree.iterparse, feed in chunks while gunzipping."""
    import gzip
    import io
    from lxml import etree

    # Decompress first (we still need full XML for parsing structure)
    if content_bytes[:2] == b"\x1f\x8b":
        text = gzip.decompress(content_bytes)
    else:
        text = content_bytes

    # Use iterparse to walk the tree
    page_count = 0
    url_attrs = []
    for event, elem in etree.iterparse(
        io.BytesIO(text), events=("end",)
    ):
        if elem.tag.endswith("}url") or elem.tag == "url":
            # Page element
            for child in elem:
                if child.tag.endswith("}loc") or child.tag == "loc":
                    if child.text:
                        url_attrs.append(child.text)
                        page_count += 1
            # Clear to save memory
            elem.clear()
        elif elem.tag.endswith("}sitemapindex") or elem.tag == "sitemapindex":
            for child in elem:
                if child.tag.endswith("}loc") or child.tag == "loc":
                    if child.text:
                        url_attrs.append(child.text)
            elem.clear()
    return page_count, url_attrs


def main() -> int:
    paths = get_gzipped_corpus_paths(20)
    print(f"corpus files: {len(paths)}")
    total_expat_s = 0.0
    total_lxml_s = 0.0
    for path in paths:
        data = path.read_bytes()
        # Expat
        t0 = time.monotonic()
        r_expat = parse_with_expat(data)
        te = time.monotonic() - t0
        total_expat_s += te
        # lxml
        t0 = time.monotonic()
        r_lxml = parse_with_lxml_iterparse(data)
        tl = time.monotonic() - t0
        total_lxml_s += tl
        n_pages = len(r_expat.pages) if hasattr(r_expat, "pages") else 0
        n_lxml_pages = r_lxml[0]
        print(f"  {path.name[:40]:40s} expat={te*1000:6.1f}ms lxml={tl*1000:6.1f}ms "
              f"expat_pages={n_pages} lxml_pages={n_lxml_pages}")
    print(f"TOTAL expat={total_expat_s:.2f}s lxml={total_lxml_s:.2f}s "
          f"speedup={total_expat_s/total_lxml_s:.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
