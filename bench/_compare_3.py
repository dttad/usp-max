"""Compare expat / lxml / Rust parsers on the same corpus files."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def parse_expat(content_bytes):
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
        url="http://x/sitemap.xml",
        recursion_level=0,
        parent_urls=set(),
        recurse_list_callback=lambda u, l, p: u,
    )
    return parser.sitemap()


def parse_lxml(content_bytes):
    from usp.fetcher.lxml_parser import parse_lxml_pages
    return parse_lxml_pages(content_bytes, "http://x/sitemap.xml")


def parse_rust(content_bytes):
    import usp_fast
    return usp_fast.parse_pages(content_bytes)


def main():
    with (Path(__file__).parent / "corpus" / "manifest.json").open() as f:
        manifest = json.load(f)
    paths = []
    for entry in manifest["entries"]:
        if entry.get("role") == "index":
            continue
        paths.append(Path(__file__).parent / "corpus" / entry["path"])
        if len(paths) >= 30:
            break

    t_expat = t_lxml = t_rust = 0.0
    for path in paths:
        data = path.read_bytes()

        t0 = time.monotonic()
        r_expat = parse_expat(data)
        t_expat += time.monotonic() - t0
        n_expat = len(r_expat.pages)

        t0 = time.monotonic()
        pages_lxml = parse_lxml(data)
        t_lxml += time.monotonic() - t0
        n_lxml = len(pages_lxml)

        t0 = time.monotonic()
        urls_rust = parse_rust(data)
        t_rust += time.monotonic() - t0
        n_rust = len(urls_rust)

        ok = "OK" if n_expat == n_lxml == n_rust else f"MISMATCH expat={n_expat} lxml={n_lxml} rust={n_rust}"
        print(f"  {path.name[:40]:40s} expat={t_expat*1000:6.0f}ms lxml={t_lxml*1000:6.0f}ms rust={t_rust*1000:5.0f}ms {ok}")

    print()
    print(f"TOTAL expat={t_expat*1000:.0f}ms  lxml={t_lxml*1000:.0f}ms  rust={t_rust*1000:.0f}ms")
    print(f"speedup rust/lxml = {t_lxml/max(t_rust, 0.001):.2f}x")
    print(f"speedup rust/expat = {t_expat/max(t_rust, 0.001):.2f}x")


if __name__ == "__main__":
    main()
