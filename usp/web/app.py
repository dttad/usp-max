"""Starlette routes for the usp-max web UI.

Endpoints
---------
``GET  /``                 — serve the SPA (``index.html``) or fallback page.
``GET  /api/health``       — ``{"ok": true, "version": ...}``
``POST /api/crawl``        — start a new crawl job.
``GET  /api/jobs``         — list jobs (most-recent first).
``GET  /api/jobs/{id}``    — snapshot of one job.
``DELETE /api/jobs/{id}``  — cancel a running job.
``GET  /api/jobs/{id}/stream`` — Server-Sent Events stream of log events.
``GET  /api/jobs/{id}/urls``  — download the discovered URLs (txt).
``GET  /api/jobs/{id}/manifest`` — JSON manifest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from usp import __version__
from usp.web.service import CrawlService, CrawlSettings, Job, get_service

log = logging.getLogger("usp-max.web.routes")

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>usp-max</title></head>
<body style="font:14px/1.4 -apple-system,system-ui,sans-serif;color:#222;
             max-width:680px;margin:48px auto;padding:0 16px">
<h1 style="margin:0 0 8px">usp-max web UI</h1>
<p>Build the frontend first:</p>
<pre style="background:#f4f4f4;padding:12px;border-radius:6px">cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>Then restart the server, or use the dev workflow:</p>
<pre style="background:#f4f4f4;padding:12px;border-radius:6px">cd web &amp;&amp; npm run dev   # vite on :5173 (proxies /api to this server)</pre>
</body></html>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status.value,
        "url": job.settings.url,
        "created_at": job.created_at,
        "settings": job.settings.as_dict(),
        "stats": job.stats.as_dict(),
        "error": job.error,
        "output_dir": str(job.output_dir),
    }


def _validate_settings(payload: dict[str, Any]) -> CrawlSettings:
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        raise HTTPException(
            status_code=400, detail="url must start with http:// or https://"
        )

    def _int(name: str, default: int, *, lo: int, hi: int) -> int:
        raw = payload.get(name, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail=f"{name} must be an integer"
            ) from None
        if not (lo <= value <= hi):
            raise HTTPException(
                status_code=400,
                detail=f"{name} must be between {lo} and {hi}",
            )
        return value

    proxy_raw = payload.get("proxy")
    proxy = proxy_raw.strip() if isinstance(proxy_raw, str) else None
    if proxy == "":
        proxy = None

    parser = (payload.get("parser") or "auto").strip()
    if parser not in ("auto", "expat"):
        raise HTTPException(status_code=400, detail="parser must be 'auto' or 'expat'")

    return CrawlSettings(
        url=url,
        concurrency=_int("concurrency", 16, lo=1, hi=512),
        fanout_cap=_int("fanout_cap", 200, lo=1, hi=10_000),
        max_depth=_int("max_depth", 16, lo=1, hi=64),
        parser=parser,
        proxy=proxy,
    )


def _sse_format(data: str) -> bytes:
    """Format one SSE ``data:`` frame (data: … \\n\\n)."""
    return f"data: {data}\n\n".encode()


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def index(request: Request) -> Response:
    """Serve the SPA ``index.html`` from disk, or a fallback message."""
    candidates = [
        _STATIC_DIR / "index.html",
        Path("./web/dist/index.html").resolve(),
    ]
    for path in candidates:
        if path.is_file():
            return FileResponse(path, media_type="text/html")
    return HTMLResponse(_INDEX_HTML, status_code=200)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "version": __version__})


async def create_crawl(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    settings = _validate_settings(payload)
    service: CrawlService = request.app.state.service
    job = service.start(settings)
    return JSONResponse(_job_to_dict(job), status_code=201)


async def list_jobs(request: Request) -> JSONResponse:
    service: CrawlService = request.app.state.service
    search = request.query_params.get("search")
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    jobs = service.list(search=search, limit=limit)
    return JSONResponse({"jobs": jobs})


async def get_job(request: Request) -> JSONResponse:
    service: CrawlService = request.app.state.service
    job = service.get(request.path_params["job_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JSONResponse(_job_to_dict(job))


async def cancel_job(request: Request) -> JSONResponse:
    service: CrawlService = request.app.state.service
    job = service.get(request.path_params["job_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    ok = service.cancel(request.path_params["job_id"])
    return JSONResponse({"id": job.id, "cancelled": ok})


async def stream_job(request: Request) -> Response:
    service: CrawlService = request.app.state.service
    job_id = request.path_params["job_id"]
    job = service.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def event_source():
        try:
            async for evt in service.stream(job_id):
                yield _sse_format(json.dumps(evt, ensure_ascii=False))
        except asyncio.CancelledError:
            log.info("SSE client disconnected for job %s", job_id)
        except Exception as ex:  # noqa: BLE001
            log.warning("SSE error for job %s: %s", job_id, ex)
            yield _sse_format(json.dumps({"type": "error", "message": str(ex)}))

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def get_urls(request: Request) -> Response:
    """Return the ``urls.txt`` snapshot for the job (live-friendly).

    The worker writes URLs as they are discovered, so this file grows
    in real time; the browser caches a non-200 only on its first miss.
    """
    service: CrawlService = request.app.state.service
    job = service.get(request.path_params["job_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    urls_path = job.output_dir / "urls.txt"
    if not urls_path.exists():
        raise HTTPException(status_code=404, detail="no urls yet")
    return FileResponse(
        urls_path,
        media_type="text/plain",
        filename=f"urls-{job.id}.txt",
    )


async def get_manifest(request: Request) -> JSONResponse:
    service: CrawlService = request.app.state.service
    job = service.get(request.path_params["job_id"])
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    manifest_path = job.output_dir / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="manifest not ready yet (job still running?)",
        )
    return JSONResponse(json.loads(manifest_path.read_text()))


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


async def _http_exception_json_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Return JSON for any ``HTTPException`` raised by routes.

    Starlette 1.6+ ships a plain-text handler for HTTPException; we
    override it so API consumers get a structured ``{"detail": ...}``
    body, matching the behaviour of FastAPI and the older Starlette.
    """
    detail = getattr(exc, "detail", str(exc))
    status_code = getattr(exc, "status_code", 500)
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        {"detail": detail},
        status_code=status_code,
        headers=headers,
    )


def create_app(
    *,
    output_root: Path | None = None,
    cors_origins: list[str] | None = None,
) -> Starlette:
    """Build the Starlette ASGI app.

    Parameters
    ----------
    output_root
        Directory under which per-job output directories are created.
        Defaults to ``./usp-web-out``.
    cors_origins
        List of origins to allow via CORS. Defaults to ``["*"]`` so the
        Vite dev server (``http://localhost:5173``) can hit the API
        during development. Tighten for production.
    """
    service = get_service(output_root=output_root)

    app = Starlette(
        debug=bool(int(os.environ.get("USP_WEB_DEBUG", "0"))),
    )
    app.state.service = service
    app.add_exception_handler(HTTPException, _http_exception_json_handler)

    origins = cors_origins or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    routes = [
        Route("/", endpoint=index, methods=["GET"]),
        Route("/api/health", endpoint=health, methods=["GET"]),
        Route("/api/crawl", endpoint=create_crawl, methods=["POST"]),
        Route("/api/jobs", endpoint=list_jobs, methods=["GET"]),
        Route("/api/jobs/{job_id}", endpoint=get_job, methods=["GET"]),
        Route("/api/jobs/{job_id}", endpoint=cancel_job, methods=["DELETE"]),
        Route(
            "/api/jobs/{job_id}/stream",
            endpoint=stream_job,
            methods=["GET"],
        ),
        Route(
            "/api/jobs/{job_id}/urls",
            endpoint=get_urls,
            methods=["GET"],
        ),
        Route(
            "/api/jobs/{job_id}/manifest",
            endpoint=get_manifest,
            methods=["GET"],
        ),
    ]

    # Serve the Vite production build if it exists.
    static_mounted = False
    for candidate in (_STATIC_DIR, Path("./web/dist").resolve()):
        if candidate.is_dir():
            app.mount(
                "/",
                StaticFiles(directory=str(candidate), html=True),
                name=f"static-{candidate.name}",
            )
            static_mounted = True
            break

    # Put the routes I registered above the StaticFiles mount so they
    # win the matching race.
    app.router.routes = routes + app.router.routes

    if not static_mounted:
        log.info(
            "no static directory found (looked for %s and ./web/dist); "
            "the index route will serve a build instructions page.",
            _STATIC_DIR,
        )

    return app
