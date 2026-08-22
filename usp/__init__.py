"""ultimate-sitemap-parser (usp-max fork).

This fork ships the async fetcher, CLI, and web UI on top of the
upstream package published on PyPI as ``ultimate-sitemap-parser``. We
extend ``__path__`` so ``from usp.fetch_parse import …`` and friends
resolve to the upstream modules when the user installs the upstream
package, while the fork-specific subpackages (``usp.cli_main``,
``usp.fetcher``, ``usp.web``) stay on the fork's own path.
"""
from __future__ import annotations

import importlib as _importlib
import os as _os
import sys as _sys
from importlib.metadata import version as _pkg_version

__version__ = "1.9.0"

_FORK_PKG_NAME = "ultimate-sitemap-parser"
try:
    _UPSTREAM_VERSION = _pkg_version(_FORK_PKG_NAME)
except Exception:
    _UPSTREAM_VERSION = None

# Extend __path__ with the upstream package directory (if installed)
# so imports like ``usp.fetch_parse`` and ``usp.tree`` keep working.
_upstream_dir = _os.path.dirname(_os.path.abspath(__file__))
for _cand in _sys.path:
    _usp_dir = _os.path.join(_cand, "usp")
    if (
        _os.path.isdir(_usp_dir)
        and _os.path.isfile(_os.path.join(_usp_dir, "fetch_parse.py"))
        and _os.path.abspath(_usp_dir) != _upstream_dir
    ):
        if _usp_dir not in __path__:
            __path__.append(_usp_dir)
        break

__all__ = ["__version__"]


def __getattr__(name: str):
    if name == "tree":
        return _importlib.import_module("usp.tree")
    raise AttributeError(f"module 'usp' has no attribute {name!r}")