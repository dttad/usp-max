"""Web UI for usp-max — paste a URL, watch the crawl live.

This package hosts a tiny Starlette + uvicorn service. The frontend is a
React + Vite SPA in :mod:`web` (see ``web/README.md``).

Run::

    python -m usp.cli serve --host 0.0.0.0 --port 8088
    # or
    usp-max serve --host 0.0.0.0 --port 8088
"""
