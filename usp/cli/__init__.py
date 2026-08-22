"""usp.cli — namespace package that combines this fork with the upstream
``ultimate-sitemap-parser`` package.

The fork ships :mod:`usp.cli._crawl`, :mod:`usp.cli._inspect` and
:mod:`usp.cli._log`; the upstream ships :mod:`usp.cli.cli`, :mod:`usp.cli._ls`
and :mod:`usp.cli._util`. We expose both via ``__path__`` so existing
``from usp.cli._util import setup_logging`` style imports keep working.
"""
from __future__ import annotations

import os as _os
import sys as _sys

_UPSTREAM_PKG_NAME = "ultimate-sitemap-parser"

# Extend __path__ with the upstream site's cli/ directory (if available).
_fork_dir = _os.path.dirname(_os.path.abspath(__file__))
for _cand in _sys.path:
    _cli_dir = _os.path.join(_cand, "usp", "cli")
    if (
        _os.path.isdir(_cli_dir)
        and _os.path.abspath(_cli_dir) != _fork_dir
    ):
        if _cli_dir not in __path__:
            __path__.append(_cli_dir)
        break