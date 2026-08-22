"""Human-friendly log formatter for usp-max.

Two formats behind a single logger:

- **TTY** (color-capable terminal): a structured two-line block per event
  with a colored level tag, a relative timestamp, the logger name
  short-form, and a `key=value ...` body.
- **non-TTY** (piped / redirected): a single-line ``ts LEVEL logger msg``
  format that `grep` and `jq` can chew on.

The same log call produces both; selection is automatic via
``sys.stdout.isatty()``.

Usage::

    from usp.cli._log import setup_logging
    setup_logging(verbosity=1)        # INFO
    log = logging.getLogger("usp-max.crawl")
    log.info("opened batch %s", path)   # format the same as before
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

# Single global "started at" so relative timestamps work.
# We do NOT use ``time.time()`` here because ``record.created`` is already
# in those units; we need a monotonic baseline compatible with
# ``record.relativeCreated``. (Python's logging module initializes its
# own monotonic clock when the first handler is created.)
_T0 = time.monotonic()


def _fmt_rel(secs: float) -> str:
    """Format a relative timestamp like ``01.234s`` / ``1m02.345s``.

    Takes ``record.relativeCreated / 1000`` (monotonic milliseconds since
    logging module load — NOT ``record.created`` which is an absolute
    Unix epoch timestamp from `time.time()`).
    """
    dt = max(0.0, secs)
    if dt < 100:
        return f"{dt:6.3f}s"
    m, s = divmod(dt, 60)
    return f"{int(m)}m{s:06.3f}s"


def _short_logger(name: str) -> str:
    """Trim ``usp-max.crawl`` → ``crawl``, ``httpx`` → ``httpx``."""
    if "." in name:
        return name.rsplit(".", 1)[-1]
    return name


# ANSI codes (only used in TTY mode)
_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_MAGENTA = "\x1b[35m"
_BLUE = "\x1b[34m"


_LEVEL_STYLE = {
    logging.DEBUG: (_DIM, "DEBUG", _MAGENTA),
    logging.INFO: (_CYAN, "INFO ", _GREEN),
    logging.WARNING: (_YELLOW, "WARN ", _YELLOW),
    logging.ERROR: (_RED, "ERROR", _RED),
    logging.CRITICAL: (_RED, "CRIT ", _RED),
}


class _PrettyFormatter(logging.Formatter):
    """The active formatter. Branched in ``__init__`` so it has no
    runtime cost per record.

    TTY format
    ---------
    ``<color> [<ts>]> <LEVEL> <logger>     <message>``

    Each log message should be formatted as a single line; messages
    with leading whitespace indent for visual nesting.

    non-TTY format
    --------------
    ``<ts> <LEVEL> <logger> <message>``
    """

    def __init__(self, use_color: bool):
        super().__init__()
        self._color = use_color

    def format(self, record: logging.LogRecord) -> str:
        # `relativeCreated` is monotonic milliseconds since logging was
        # first used in this process. Convert to seconds for display.
        rel = _fmt_rel(record.relativeCreated / 1000.0)
        style, level_str, accent = _LEVEL_STYLE.get(
            record.levelno, ("", "INFO ", _CYAN)
        )
        logger_short = _short_logger(record.name)

        msg = record.getMessage()
        # Indented continuation lines keep their indent.
        if "\n" in msg:
            first, *rest = msg.split("\n")
            msg = first + "\n" + "\n".join(("    " + r) for r in rest)

        if self._color:
            tag = (
                f"{style}{_BOLD}{level_str}{_RESET} "
                f"{style}[{rel}]{_RESET} "
                f"{accent}{logger_short:<12}{_RESET} "
            )
        else:
            tag = f"[{rel}] {level_str} {logger_short:<12} "

        return tag + msg


class _TerseFilter(logging.Filter):
    """Hide noisy third-party DEBUG/INFO chatter unless verbosity=2."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        # Hide noisy libraries unless we explicitly turned DEBUG on.
        if record.name.startswith(("httpx", "httpcore", "h2", "anyio")):
            return record.levelno >= logging.INFO
        return True


def setup_logging(verbosity: int = 1, log_path: str | None = None) -> None:
    """Configure the root logger for usp-max.

    ``verbosity``:
      0 — WARNING only (--quiet)
      1 — INFO + WARNING (default)
      2 — DEBUG + everything (-v)

    ``log_path``: optional file path; if set, only INFO+ goes to the
    file with the terse (non-TTY) format, while the console stays
    pretty.
    """
    level = {
        0: logging.WARNING,
        1: logging.INFO,
        2: logging.DEBUG,
    }.get(verbosity, logging.INFO)

    root = logging.getLogger()
    # Wipe any existing handlers (uvicorn / pytest may have set some).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG if verbosity >= 2 else logging.INFO)

    fmt_tty = _PrettyFormatter(use_color=_isatty())
    fmt_plain = _PrettyFormatter(use_color=False)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(level)
    console.setFormatter(fmt_tty)
    console.addFilter(_TerseFilter())
    root.addHandler(console)

    if log_path:
        fh = logging.FileHandler(log_path)
        fh.setLevel(level)
        fh.setFormatter(fmt_plain)
        root.addHandler(fh)


def _isatty() -> bool:
    """``True`` if stderr is a terminal and ``NO_COLOR`` is unset."""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


# Convenience: a logger pre-bound to the ``usp-max`` package.
log = logging.getLogger("usp-max")


def kv(**fields: Any) -> str:
    """Build a ``key=value key=value ...`` string for log messages.

    Values are rendered with ``str()``; ``None`` becomes ``null``;
    strings with whitespace are quoted so ``grep "key=foo"`` still works.
    """
    out: list[str] = []
    for k, v in fields.items():
        if v is None:
            out.append(f"{k}=null")
        elif isinstance(v, str) and any(c.isspace() for c in v):
            out.append(f'{k}="{v}"')
        else:
            out.append(f"{k}={v}")
    return " ".join(out)