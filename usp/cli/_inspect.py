"""Inspect a finished crawl output directory.

Subcommands that the Dockerfile ENTRYPOINT (`python -m usp.cli_main`)
hands off to. Each one runs entirely inside the Docker image so we
never need zstd/tar/jq locally.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import tarfile
import time
import zstandard

from usp.cli._util import setup_logging


def _iter_batch_files(out_dir) -> list:
    out = []
    for path in sorted(out_dir.iterdir()):
        name = path.name
        if not (name.startswith("urls-") and ".txt" in name):
            continue
        out.append(path)
    return out


def _open_inner(path) -> tuple[str, bytes]:
    """Return (inner_name, raw_url_bytes) for one batch file."""
    name = path.name
    raw = path.read_bytes()

    if name.endswith(".tar.gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            tar_bytes = gz.read()
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            member = next((m for m in tar.getmembers() if m.isfile()), None)
            if member is None:
                return name, b""
            inner_name = member.name
            with tar.extractfile(member) as f:
                return inner_name, (f.read() if f else b"")
    if name.endswith(".tar.zst"):
        dctx = zstandard.ZstdDecompressor()
        with dctx.stream_reader(io.BytesIO(raw)) as reader:
            tar_bytes = reader.read()
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            member = next((m for m in tar.getmembers() if m.isfile()), None)
            if member is None:
                return name, b""
            inner_name = member.name
            with tar.extractfile(member) as f:
                return inner_name, (f.read() if f else b"")
    if name.endswith(".tar"):
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            member = next((m for m in tar.getmembers() if m.isfile()), None)
            if member is None:
                return name, b""
            inner_name = member.name
            with tar.extractfile(member) as f:
                return inner_name, (f.read() if f else b"")
    if name.endswith(".gz"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            return name, gz.read()
    if name.endswith(".zst"):
        dctx = zstandard.ZstdDecompressor()
        with dctx.stream_reader(io.BytesIO(raw)) as reader:
            return name, reader.read()
    return name, raw


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "ls",
        help="List batch files in an output directory",
        description="Print a compact listing of every urls-* batch file in OUT.",
    )
    p.add_argument("out_dir", help="Output directory produced by `crawl`")
    p.set_defaults(func=_run_ls)

    p = subparsers.add_parser(
        "wc",
        help="Count URLs in each batch file (no extraction to disk)",
        description=(
            "Stream-decompress each batch file and count newlines. "
            "Stays inside the Docker image so no local zstd/tar is needed."
        ),
    )
    p.add_argument("out_dir", help="Output directory produced by `crawl`")
    p.set_defaults(func=_run_wc)

    p = subparsers.add_parser(
        "manifest",
        help="Pretty-print manifest.json",
        description="Reads $(OUT)/manifest.json and pretty-prints it.",
    )
    p.add_argument("out_dir", help="Output directory produced by `crawl`")
    p.set_defaults(func=_run_manifest)

    p = subparsers.add_parser(
        "extract",
        help="Extract every batch to a directory of plain txt files",
        description=(
            "Stream-decompresses every batch under OUT and writes plain "
            "txt files under TO. Stays inside the Docker image."
        ),
    )
    p.add_argument("out_dir", help="Output directory produced by `crawl`")
    p.add_argument("to_dir", help="Where to write the extracted txt files")
    p.set_defaults(func=_run_extract)


def _run_ls(args: argparse.Namespace) -> None:
    from pathlib import Path
    out = Path(args.out_dir)
    if not out.is_dir():
        print(f"error: {out} is not a directory", file=sys.stderr)
        sys.exit(1)
    files = _iter_batch_files(out)
    if not files:
        print(f"(no batch files in {out})")
        return
    print(f"{'name':40s} {'size':>10s} {'format':10s}")
    for f in files:
        kind = "plain"
        n = f.name
        if n.endswith(".tar.gz"):
            kind = "tar.gz"
        elif n.endswith(".tar.zst"):
            kind = "tar.zst"
        elif n.endswith(".tar"):
            kind = "tar"
        elif n.endswith(".gz"):
            kind = "gz"
        elif n.endswith(".zst"):
            kind = "zst"
        print(f"{n:40s} {f.stat().st_size:>10d} {kind:10s}")


def _run_wc(args: argparse.Namespace) -> None:
    from pathlib import Path
    out = Path(args.out_dir)
    if not out.is_dir():
        print(f"error: {out} is not a directory", file=sys.stderr)
        sys.exit(1)
    files = _iter_batch_files(out)
    if not files:
        print(f"(no batch files in {out})")
        return
    grand = 0
    print(f"{'name':40s} {'urls':>10s}")
    for f in files:
        inner_name, raw = _open_inner(f)
        n = raw.count(b"\n") if raw else 0
        grand += n
        print(f"{f.name:40s} {n:>10d}")
    print(f"{'-' * 52}")
    print(f"{'TOTAL':40s} {grand:>10d}")


def _run_manifest(args: argparse.Namespace) -> None:
    from pathlib import Path
    out = Path(args.out_dir)
    manifest = out / "manifest.json"
    if not manifest.is_file():
        print(f"error: {manifest} not found. Run a crawl first.", file=sys.stderr)
        sys.exit(1)
    data = json.loads(manifest.read_text())
    print(json.dumps(data, indent=2, sort_keys=True))


def _run_extract(args: argparse.Namespace) -> None:
    """Extract every batch to a directory of plain txt files."""
    import os
    from pathlib import Path
    out = Path(args.out_dir)
    to = Path(args.to_dir)
    if not out.is_dir():
        print(f"error: {out} is not a directory", file=sys.stderr)
        sys.exit(1)
    to.mkdir(parents=True, exist_ok=True)
    files = _iter_batch_files(out)
    if not files:
        print(f"(no batch files in {out})")
        return
    grand = 0
    started = time.monotonic()
    for f in files:
        inner_name, raw = _open_inner(f)
        # inner_name is urls-NNNNN.txt or urls-NNNNN.txt.gz (we already
        # decompressed). For .gz / .zst without tar the inner is a txt;
        # for .tar.* the inner is whatever was put inside (txt).
        out_name = inner_name
        # If inner_name still ends in .gz / .zst, strip that suffix.
        for suf in (".gz", ".zst"):
            if out_name.endswith(suf):
                out_name = out_name[: -len(suf)]
        target = to / out_name
        target.write_bytes(raw)
        grand += raw.count(b"\n")
        print(f"  {f.name} -> {target.relative_to(to)}  ({raw.count(b'\\n')} urls)")
    print(f"extracted {grand} urls from {len(files)} batches in {time.monotonic() - started:.1f}s")