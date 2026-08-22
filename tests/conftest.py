"""Shared pytest fixtures and config."""
import pytest

# Make async tests work without per-test decoration
def pytest_collection_modifyitems(config, items):
    for item in items:
        if "asyncio" in item.keywords:
            continue
