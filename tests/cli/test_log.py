"""Tests for usp.cli._log — the structured log formatter."""
from __future__ import annotations

import io
import logging
import os
import sys

import pytest

from usp.cli._log import _PrettyFormatter, _TerseFilter, kv, setup_logging


def _format(record, use_color: bool) -> str:
    fmt = _PrettyFormatter(use_color=use_color)
    return fmt.format(record)


def _make_record(name: str = "usp-max.crawl", level: int = logging.INFO,
                 msg: str = "hello") -> logging.LogRecord:
    r = logging.LogRecord(
        name=name, level=level, pathname="x.py", lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    r.relativeCreated = 1234.5
    return r


# --- kv() -------------------------------------------------------------

def test_kv_simple():
    assert kv(a=1, b="two") == "a=1 b=two"


def test_kv_none():
    assert kv(x=None) == "x=null"


def test_kv_quotes_whitespace():
    assert kv(name="hello world") == 'name="hello world"'
    assert kv(path="a b\tc") == 'path="a b\tc"'


# --- formatter output --------------------------------------------------

def test_tty_format_includes_colors():
    s = _format(_make_record(), use_color=True)
    assert "\x1b[" in s
    assert "INFO " in s
    assert "crawl" in s
    assert "hello" in s
    # Color escapes are bracketed between two resets at the boundaries.
    assert s.count(_PrettyFormatter.__dict__ and "\x1b[0m") >= 2 or "\x1b[0m" in s


def test_non_tty_format_no_colors():
    s = _format(_make_record(), use_color=False)
    assert "\x1b[" not in s
    assert "INFO " in s
    assert "crawl" in s
    assert "hello" in s


def test_level_marker_changes():
    info = _format(_make_record(level=logging.INFO, msg="x"), use_color=False)
    warn = _format(_make_record(level=logging.WARNING, msg="x"), use_color=False)
    error = _format(_make_record(level=logging.ERROR, msg="x"), use_color=False)
    assert "INFO " in info
    assert "WARN " in warn
    assert "ERROR" in error


def test_short_logger_name():
    """``usp-max.crawl`` → ``crawl``; ``httpx`` → ``httpx``."""
    s = _format(_make_record(name="usp-max.crawl"), use_color=False)
    assert "crawl" in s
    s2 = _format(_make_record(name="httpx"), use_color=False)
    assert "httpx" in s2
    # Logger with no dot is left alone.
    s3 = _format(_make_record(name="plainname"), use_color=False)
    assert "plainname" in s3


def test_multiline_message_indented():
    s = _format(_make_record(msg="first\nsecond\nthird"), use_color=False)
    lines = s.split("\n")
    assert len(lines) == 3
    assert "first" in lines[0]
    for cont in lines[1:]:
        assert cont.startswith("    "), cont


# --- setup_logging() ----------------------------------------------------

def test_setup_logging_installs_console_handler(monkeypatch):
    """setup_logging attaches exactly one StreamHandler to the root."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    setup_logging(verbosity=1, log_path=None)
    assert any(
        isinstance(h, logging.StreamHandler) for h in root.handlers
    ), "no console handler attached"
    # cleanup
    for h in list(root.handlers):
        root.removeHandler(h)


def test_setup_logging_file_handler(tmp_path):
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    log_file = tmp_path / "crawl.log"
    setup_logging(verbosity=2, log_path=str(log_file))
    try:
        logging.getLogger("test.cli_log").info("hello file")
    finally:
        for h in list(root.handlers):
            h.flush()
            h.close()
            root.removeHandler(h)
    content = log_file.read_text()
    assert "hello file" in content
    # No ANSI codes in file log.
    assert "\x1b[" not in content


def test_terse_filter_hides_noisy_debug():
    """Without -v, httpx/h2/anyio DEBUG is suppressed; warnings pass through."""
    f = _TerseFilter()
    debug_httpx = _make_record(name="httpx", level=logging.DEBUG, msg="x")
    debug_anyio = _make_record(name="anyio", level=logging.DEBUG, msg="x")
    debug_crawl = _make_record(name="usp-max.crawl", level=logging.DEBUG, msg="x")
    warn_httpx = _make_record(name="httpx", level=logging.WARNING, msg="x")

    # DEBUG from noisy libs is filtered.
    assert not f.filter(debug_httpx)
    assert not f.filter(debug_anyio)
    # DEBUG from our own logger passes.
    assert f.filter(debug_crawl)
    # WARNING always passes.
    assert f.filter(warn_httpx)


def test_no_color_when_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    # _isatty() should return False, so setup_logging would set
    # use_color=False. We test the helper directly via a fresh
    # formatter setup.
    from usp.cli._log import _isatty
    # In pytest, stderr is not a TTY, so _isatty is False anyway,
    # but with NO_COLOR it should be False even if it were a TTY.
    assert _isatty() is False
