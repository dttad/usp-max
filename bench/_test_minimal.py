"""Compare different lxml parse strategies."""
from __future__ import annotations

import time
import gzip
import json
from pathlib import Path
from lxml import etree
import io


def _local(tag):
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


# Strategy 1: full iterparse (current lxml_parser.py approach)
def parse_full(content: bytes):
    pages = []
    if content[:2] == b"\x1f\x8b":
        text = gzip.decompress(content)
    else:
        text = content
    current = None
    PAGE_TAGS = {"loc", "lastmod", "priority", "changefreq", "image", "news"}
    for event, elem in etree.iterparse(io.BytesIO(text), events=("start", "end"), huge_tree=True):
        local = _local(elem.tag)
        if event == "start":
            if local == "url" and current is None:
                current = {"loc": None}
        else:
            if local == "url":
                if current and current.get("loc"):
                    pages.append(current["loc"])
                current = None
            elif local in PAGE_TAGS:
                if current is not None and local == "loc":
                    if current["loc"] is None:
                        current["loc"] = (elem.text or "").strip()
            elem.clear()
    return pages


# Strategy 2: only listen for end of <loc> inside <url>
def parse_minimal(content: bytes):
    pages = []
    if content[:2] == b"\x1f\x8b":
        text = gzip.decompress(content)
    else:
        text = content
    in_url = False
    saw_loc_text = False
    pending_loc = ""
    for event, elem in etree.iterparse(io.BytesIO(text), events=("end",), huge_tree=True):
        local = _local(elem.tag)
        if local == "url":
            if in_url and pending_loc:
                pages.append(pending_loc)
            in_url = True
            pending_loc = ""
            saw_loc_text = False
            elem.clear()  # clear children
        elif local == "loc" and in_url and not saw_loc_text:
            text_val = (elem.text or "")
            pending_loc = text_val
            saw_loc_text = True
            elem.clear()
        else:
            elem.clear()
    if in_url and pending_loc:
        pages.append(pending_loc)
    return pages


def main():
    with open("bench/corpus/manifest.json") as f:
        manifest = json.load(f)

    paths = []
    for entry in manifest["entries"]:
        if entry.get("role") == "index":
            continue
        paths.append(Path("bench/corpus") / entry["path"])
        if len(paths) >= 30:
            break

    t1 = t2 = 0.0
    for path in paths:
        data = path.read_bytes()

        t0 = time.monotonic()
        p1 = parse_full(data)
        t1 += time.monotonic() - t0

        t0 = time.monotonic()
        p2 = parse_minimal(data)
        t2 += time.monotonic() - t0

        if p1 != p2:
            print(f"  MISMATCH {path.name}: full={len(p1)} minimal={len(p2)}")

    print(f"\nTOTAL full={t1*1000:.0f}ms minimal={t2*1000:.0f}ms")
    print(f"Speedup minimal/full: {t1/max(t2, 0.001):.2f}x")


if __name__ == "__main__":
    main()
