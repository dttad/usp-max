"""Compare expat vs lxml parsers on the same corpus files."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def parse_with_expat(content_bytes: bytes):
    import gzip
    from usp.fetch_parse import XMLSitemapParser
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


def main() -> int:
    from usp.fetcher.lxml_parser import parse_lxml_pages, parse_lxml_index
    from usp.objects.sitemap import PagesXMLSitemap, IndexXMLSitemap

    with (Path(__file__).parent / "corpus" / "manifest.json").open() as f:
        manifest = json.load(f)
    paths = []
    for entry in manifest["entries"]:
        if entry.get("role") == "index":
            continue
        paths.append(Path(__file__).parent / "corpus" / entry["path"])
        if len(paths) >= 20:
            break

    total_expat = total_lxml = 0.0
    matches = 0
    total = 0
    for path in paths:
        data = path.read_bytes()
        # Expat
        t0 = time.monotonic()
        try:
            r_expat = parse_with_expat(data)
            te = time.monotonic() - t0
        except Exception as e:
            te = 0.0
            r_expat = None
            print(f"  expat FAIL {path.name}: {e}")
            continue
        total_expat += te
        n_expat = len(r_expat.pages) if hasattr(r_expat, "pages") else 0

        # lxml
        t0 = time.monotonic()
        try:
            pages = parse_lxml_pages(data, "http://example.com/sitemap.xml")
            tl = time.monotonic() - t0
            r_lxml = PagesXMLSitemap(url="http://example.com/sitemap.xml", pages=pages)
        except Exception as e:
            tl = 0.0
            r_lxml = None
            print(f"  lxml FAIL {path.name}: {e}")
            continue
        total_lxml += tl
        n_lxml = len(pages)
        if n_expat == n_lxml:
            matches += 1
        total += 1
        print(f"  {path.name[:40]:40s} expat={te*1000:6.1f}ms lxml={tl*1000:6.1f}ms "
              f"expat_pages={n_expat} lxml_pages={n_lxml}")

    print(f"TOTAL expat={total_expat:.2f}s lxml={total_lxml:.2f}s "
          f"speedup={total_expat/max(total_lxml, 0.001):.2f}x "
          f"page_count_match={matches}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
