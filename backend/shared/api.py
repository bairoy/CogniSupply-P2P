"""
Shared FastAPI app factory. Every HTTP service (Yard API, Procurement API,
Dashboard Gateway) is built with create_app() so cross-cutting concerns are
configured identically in one place rather than copy-pasted three times.

What it wires up:

1. CORS. The React frontend runs on a different origin (:5173 in dev), so
   without this every browser request is blocked before it reaches a handler.
2. Authentication. One middleware rejects unauthenticated requests before any
   handler runs, so a new endpoint is protected by default -- forgetting to
   add a dependency cannot silently leave a route open. Per-endpoint ROLE
   checks are still explicit (Depends(require(...)), see shared/auth.py).
3. GET /health. Checks Postgres AND Redis, not just "the process is alive" --
   a service that can't reach its database is not healthy, and docker-compose
   healthchecks depend on this telling the truth.
4. A consistent JSON error envelope, so the frontend has one error shape to
   handle instead of three.
"""

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
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

    from shared.auth import DEV_SECRET, JWT_SECRET, is_public, user_from_header

    if JWT_SECRET == DEV_SECRET:
        logger.warning(
            "%s is using the built-in DEV JWT secret. Set JWT_SECRET in .env "
            "before this runs anywhere but a laptop.", title,
        )

    # ---- middleware order matters ----
    # Starlette runs the LAST-registered middleware OUTERMOST. Auth is
    # registered first and CORS second, so CORS wraps auth. That ordering is
    # load-bearing, not cosmetic:
    #   - a CORS preflight (OPTIONS, which carries no Authorization header) is
    #     answered by CORSMiddleware and never reaches the auth check;
    #   - a 401 still comes back with Access-Control-Allow-Origin, so the
    #     browser lets the frontend read the status. Reverse the order and
    #     every auth failure surfaces in the UI as an opaque network error.

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        """
        Authenticate every request except the public allowlist, and stash the
        decoded caller on request.state for shared.auth.current_user().

        Protected-by-default: a route is only reachable anonymously if its path
        is in shared.auth.PUBLIC_PATHS/PUBLIC_PREFIXES.
        """
        if request.method == "OPTIONS" or is_public(request.url.path):
            return await call_next(request)

        try:
            request.state.user = user_from_header(request.headers.get("authorization"))
        except HTTPException as exc:
            # Raising from middleware bypasses FastAPI's exception handlers,
            # so build the same JSON envelope the rest of the API returns.
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": "Unauthorized", "detail": exc.detail},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)

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
