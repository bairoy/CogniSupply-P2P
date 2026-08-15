"""
Shared FastAPI app factory. Every HTTP service (Yard API, Procurement API,
Dashboard Gateway) is built with create_app() so cross-cutting concerns are
configured identically in one place rather than copy-pasted three times.

What it wires up:

1. CORS. The React frontend runs on a different origin (:5173 in dev), so
   without this every browser request is blocked before it reaches a handler.
2. GET /health. Checks Postgres AND Redis, not just "the process is alive" --
   a service that can't reach its database is not healthy, and docker-compose
   healthchecks depend on this telling the truth.
3. A consistent JSON error envelope, so the frontend has one error shape to
   handle instead of three.
"""

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

logger = logging.getLogger("api")

# Dev origins for the Vite frontend. Vite picks the next free port when 5173 is
# taken, so allow the small range it actually uses rather than pinning one.
ALLOWED_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173,"
    "http://localhost:5174,http://127.0.0.1:5174,"
    "http://localhost:4173,http://127.0.0.1:4173",
).split(",")


def create_app(title: str, description: str = "", version: str = "1.0.0") -> FastAPI:
    app = FastAPI(title=title, description=description, version=version)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["meta"])
    def health():
        """Liveness + dependency check. Reports per-dependency status."""
        from shared.db import get_conn

        status = {"service": title, "status": "ok", "postgres": "unknown", "redis": "unknown"}

        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                conn.rollback()  # release the read txn cleanly
            status["postgres"] = "ok"
        except Exception as exc:
            status["postgres"] = f"error: {exc.__class__.__name__}"
            status["status"] = "degraded"

        try:
            from event_bus import r

            r.ping()
            status["redis"] = "ok"
        except Exception as exc:
            status["redis"] = f"error: {exc.__class__.__name__}"
            status["status"] = "degraded"

        return status

    @app.exception_handler(Exception)
    def unhandled_exception_handler(request: Request, exc: Exception):
        # Without this, an unexpected error returns an HTML traceback page that
        # the frontend's response.json() chokes on, hiding the real cause.
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": exc.__class__.__name__, "detail": str(exc)},
        )

    return app
