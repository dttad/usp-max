"""Shared fixtures for the usp.web tests.

We use Starlette's :class:`TestClient` (which wraps httpx) so the tests
exercise the real ASGI flow including middleware (CORS) and SSE.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from usp.web import service as _service
from usp.web.app import create_app


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    p = tmp_path / "usp-web-out"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def app(output_root: Path):
    """Build a fresh Starlette app for each test."""
    _service.reset_service()
    return create_app(output_root=output_root)


@pytest.fixture
def client(app):
    from starlette.testclient import TestClient

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_service(output_root: Path):
    """Drop the singleton between tests so output_root stays per-test."""
    _service.reset_service()
    yield
    _service.reset_service()
