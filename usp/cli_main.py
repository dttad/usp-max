"""usp-max command-line entry point.

Top-level: ``usp-max crawl <URL> [OPTIONS]``.
"""
from __future__ import annotations

import argparse
import sys

from usp import __version__
from usp.cli import _crawl as crawl_cmd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usp-max",
        description="usp-max — high-performance sitemap crawler",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s v{__version__}"
    )
    sub = parser.add_subparsers(required=False, metavar="")
    crawl_cmd.register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())