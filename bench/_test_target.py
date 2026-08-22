"""Test lxml target parser for speed."""
from __future__ import annotations

import sys
import time
import io
import gzip
from pathlib import Path
import json
from lxml import etree


class _Target:
    """SAX-like target for lxml.etree.XMLParser."""

    __slots__ = ("pages", "current", "saw_doc", "_stack", "_nsmap_local")

    def __init__(self):
        self.pages = []
        self.current = None
        self.saw_doc = False
        self._stack = []
        self._nsmap_local = {}

    def start(self, tag, attrs):
        local = _strip_ns(tag)
        # Track stack to know when we're inside a <url>
        self._stack.append(local)
        if local == "url":
            self.current = {
                "loc": None, "lastmod": None, "priority": None,
                "changefreq": None,
            }

    def end(self, tag):
        local = _strip_ns(tag)
        if self._stack and self._stack[-1] == local:
            self._stack.pop()
        if local == "url":
            if self.current and self.current.get("loc"):
                self.pages.append(self.current["loc"])
            self.current = None
        elif self.current is not None and local == "loc":
            # text was already captured by data()
            pass

    def data(self, data):
        if not self._stack:
            return
        top = self._stack[-1]
        if top == "loc" and self.current is not None:
            if self.current["loc"] is None:
                self.current["loc"] = data
            else:
                self.current["loc"] += data

    def close(self):
        pass


def _strip_ns(tag):
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def parse_target(content: bytes) -> list[str]:
    target = _Target()
    parser = etree.XMLParser(target=target, recover=False)
    if content[:2] == b"\x1f\x8b":
        text = gzip.decompress(content)
    else:
        text = content
    parser.feed(text)
    parser.close()
    return target.pages


def parse_iterparse(content: bytes) -> list[str]:
    pages = []
    if content[:2] == b"\x1f\x8b":
        text = gzip.decompress(content)
    else:
        text = content
    for _event, elem in etree.iterparse(io.BytesIO(text), events=("end",)):
        local = _strip_ns(elem.tag)
        if local == "loc":
            text = (elem.text or "")
            pages.append(text)
            elem.clear()
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

    t_iterparse = 0.0
    t_target = 0.0
    for path in paths:
        data = path.read_bytes()

        t0 = time.monotonic()
        pages_iter = parse_iterparse(data)
        t_iterparse += time.monotonic() - t0

        t0 = time.monotonic()
        pages_target = parse_target(data)
        t_target += time.monotonic() - t0

        if pages_iter != pages_target:
            print(f"  MISMATCH {path.name}: iterparse={len(pages_iter)} target={len(pages_target)}")

    print(f"\nTOTAL iterparse={t_iterparse*1000:.0f}ms target={t_target*1000:.0f}ms")
    print(f"Speedup: {t_iterparse/max(t_target, 0.001):.2f}x")


if __name__ == "__main__":
    main()
