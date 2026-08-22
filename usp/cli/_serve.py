"""``usp-max serve`` — run the web UI.

Examples
--------
::

    # Default: localhost:8088
    usp-max serve

    # Bind on all interfaces
    usp-max serve --host 0.0.0.0 --port 8088

    # Persist crawl outputs under a known directory
    usp-max serve --output-dir /var/lib/usp-web

    # Disable reload (production-style)
    usp-max serve --no-reload
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from usp.cli._log import kv, setup_logging

log = logging.getLogger("usp-max.serve")


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "serve",
        help="Run the web UI (paste a URL, watch it crawl)",
        description=(
            "Start a Starlette + uvicorn service that hosts the usp-max "
            "React UI. Open the printed URL in a browser, paste a "
            "homepage, and watch the crawl run with live log streaming."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--host",
        default="0.0.0.0",
        help="Interface to bind (default: 0.0.0.0)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8088,
        help="TCP port (default: 8088)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./usp-web-out"),
        help="Where crawl outputs are written (default: ./usp-web-out)",
    )
    p.add_argument(
        "--reload",
        action="store_true",
        help="Enable autoreload (development use only)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="uvicorn worker count (default: 1; bump only with --reload=off)",
    )
    p.add_argument(
        "--no-reload",
        dest="reload",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress access logs",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    verbosity = 0 if args.quiet else 1
    setup_logging(verbosity=verbosity, log_path=None)

    import uvicorn

    log.info("→ usp-max serve")
    log.info(
        "  %s",
        kv(
            host=args.host,
            port=args.port,
            output_dir=str(args.output_dir),
            reload=args.reload,
            workers=args.workers,
        ),
    )
    log.info(
        "  open:  http://%s:%d/",
        "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host,
        args.port,
    )

    # Import inside run() so the CLI startup is snappy and so the
    # service module isn't loaded for users who only run ``crawl``.
    from usp.web.app import create_app

    app = create_app(output_root=args.output_dir)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
        log_level="warning" if args.quiet else "info",
        access_log=not args.quiet,
    )
