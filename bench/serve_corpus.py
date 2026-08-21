"""Local HTTP server that replays the corpus with configurable latency.

Profiles (see MASTER-PLAN §2.1):
- zero:      0ms +/- 0
- lan:       5ms +/- 2ms
- vn-google: 180ms +/- 40ms
- hostile:   250ms +/- 150ms, 5% returns 429, 2% timeout

Usage:
    uv run bench/serve_corpus.py --profile vn-google --port 8765
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CORPUS_DIR = Path(__file__).parent / "corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"

PROFILES = {
    "zero":      {"mean_ms": 0,    "jitter_ms": 0,    "rate_429": 0.0, "timeout_ms": 0},
    "lan":       {"mean_ms": 5,    "jitter_ms": 2,    "rate_429": 0.0, "timeout_ms": 0},
    "vn-google": {"mean_ms": 180,  "jitter_ms": 40,   "rate_429": 0.0, "timeout_ms": 0},
    "hostile":   {"mean_ms": 250,  "jitter_ms": 150,  "rate_429": 0.05, "timeout_ms": 2000},
}

log = logging.getLogger("bench.serve_corpus")


def load_manifest():
    """Returns (path_map, robots_txt) where path_map is path -> bytes, and robots_txt is synthesised."""
    if not MANIFEST_PATH.exists():
        raise SystemExit(f"manifest not found: {MANIFEST_PATH}")
    with MANIFEST_PATH.open() as f:
        manifest = json.load(f)
    path_map: dict[str, bytes] = {}
    index_urls: list[str] = []
    for entry in manifest["entries"]:
        url = entry["url"]
        path = urlparse(url).path
        file_path = CORPUS_DIR / entry["path"]
        if not file_path.exists():
            log.error("missing corpus file: %s", file_path)
            continue
        body = file_path.read_bytes()
        path_map[path] = body
        if entry.get("role") == "index":
            index_urls.append(url)
    robots_txt = _build_robots_txt(index_urls)
    log.info("loaded %d path entries, %d index URLs",
             len(path_map), len(index_urls))
    return path_map, robots_txt


def _build_robots_txt(index_urls: list[str]) -> bytes:
    lines = ["User-agent: *", "Allow: /"]
    for u in index_urls:
        lines.append(f"Sitemap: {u}")
    return ("\n".join(lines) + "\n").encode()


class _BenchHandler(BaseHTTPRequestHandler):
    server_version = "USPBenchReplay/0.1"
    profile: dict = PROFILES["zero"]
    rng: random.Random = random.Random()
    path_map: dict[str, bytes] = {}
    robots_txt: bytes = b""

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - " + fmt, self.address_string(), *args)

    def _send(self, status: int, body: bytes, content_type: str, *, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        profile = self.profile
        rng = self.rng

        # Latency before serving
        delay_ms = max(0.0, profile["mean_ms"] + rng.uniform(-profile["jitter_ms"], profile["jitter_ms"]))

        # Simulate timeout (close connection without responding)
        if profile["timeout_ms"] and delay_ms > profile["timeout_ms"]:
            log.debug("SIM timeout %s (%.0fms)", path, delay_ms)
            try:
                self.close_connection = True
            except Exception:
                pass
            time.sleep(delay_ms / 1000.0)
            return

        time.sleep(delay_ms / 1000.0)

        # Simulate 429
        if rng.random() < profile["rate_429"]:
            retry_after = rng.randint(1, 5)
            self.send_response(429)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Retry-After", str(retry_after))
            self.send_header("Content-Length", "0")
            self.end_headers()
            log.debug("SIM 429 %s (retry-after=%d)", path, retry_after)
            return

        # Special-case robots.txt
        if path == "/robots.txt":
            self._send(200, self.robots_txt, "text/plain")
            log.debug("200 robots.txt (%d bytes)", len(self.robots_txt))
            return

        # Lookup by exact path
        body = self.path_map.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "0")
            self.end_headers()
            log.debug("404 %s", path)
            return

        if body[:2] == b"\x1f\x8b":
            content_type = "application/x-gzip"
        elif body.startswith(b"<?xml"):
            content_type = "application/xml"
        else:
            content_type = "application/octet-stream"

        extra = {}
        if rng.random() < 0.3:
            extra["ETag"] = '"' + hashlib.md5(body).hexdigest() + '"'

        self._send(200, body, content_type, extra_headers=extra)
        log.debug("200 %s %d bytes", path, len(body))


def make_server(profile_name: str, host: str, port: int) -> ThreadingHTTPServer:
    if profile_name not in PROFILES:
        raise SystemExit(f"unknown profile: {profile_name}; choices: {list(PROFILES)}")
    rng = random.Random(0xC0FFEE)
    path_map, robots_txt = load_manifest()
    _BenchHandler.profile = PROFILES[profile_name]
    _BenchHandler.rng = rng
    _BenchHandler.path_map = path_map
    _BenchHandler.robots_txt = robots_txt

    class Bound(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Bound((host, port), _BenchHandler)
    return server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="zero", choices=list(PROFILES))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log.upper()),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    server = make_server(args.profile, args.host, args.port)
    log.info("serving corpus on http://%s:%d profile=%s",
             args.host, args.port, args.profile)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
