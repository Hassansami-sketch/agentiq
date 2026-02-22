"""
core/exception_handlers.py
Global FastAPI exception handlers.

Register these in main.py:
    from core.exception_handlers import register_exception_handlers
    register_exception_handlers(app)

Handles:
  - RequestValidationError  → 422 with field-level details
  - HTTPException           → pass-through with logging
  - Exception (catch-all)  → 500 without leaking internals to client
"""
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:

    # ── 422: Pydantic / body validation ──────────────────────────────────────
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        logger.warning(
            "Validation error | %s %s | %s",
            request.method, request.url.path, exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Request validation failed",
                "errors": exc.errors(),
                "body":   str(exc.body)[:500] if exc.body else None,
            }
        )

    # ── 4xx: HTTP errors — log 500+, pass-through rest ───────────────────────
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code >= 500:
            logger.error(
                "HTTP %d | %s %s | %s",
                exc.status_code, request.method, request.url.path, exc.detail
            )
        else:
            logger.info(
                "HTTP %d | %s %s | %s",
                exc.status_code, request.method, request.url.path, exc.detail
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    # ── 500: Unhandled — NEVER leak stack traces to the client ────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(
            "Unhandled exception | %s %s | %s",
            request.method, request.url.path, exc,
            exc_info=True,  # includes full traceback in logs
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error. Check server logs for details.",
                "path": str(request.url.path),
            }
        )
