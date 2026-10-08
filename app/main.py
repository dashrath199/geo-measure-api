"""FastAPI application factory and startup wiring.

Creates the application, configures logging, registers routers and defines
the startup/shutdown lifespan. No business logic lives here.

Error contract
--------------
Every error response — domain, validation, routing or unexpected — uses
one JSON envelope::

    {"error": {"code": "UNSUPPORTED_FILE_TYPE", "message": "..."}}

``code`` is machine-readable (from the exception's class, see
``app.exceptions``); ``message`` is human-readable, redacted of server
paths and never a stack trace. Unexpected exceptions are logged with
full tracebacks for operators before the generic client response.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.requests import Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import __version__
from app.api.routes.files import router as files_router
from app.api.routes.health import router as health_router
from app.config import Settings, get_settings
from app.db.base import init_db
from app.exceptions import FileTooLargeError, GeoAppError, redact_paths
from app.geo.environment import verify_geospatial_environment
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)

# Generic fallback for unexpected failures: no internals, ever.
_GENERIC_SERVER_ERROR = (
    "An unexpected error occurred. Please try again later."
)

# Routing/framework errors → stable codes (status speaks for itself).
_HTTP_CODE_BY_STATUS: dict[int, str] = {
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    406: "NOT_ACCEPTABLE",
    413: "FILE_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    429: "TOO_MANY_REQUESTS",
}


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Build the API-wide JSON error envelope.

    The message is passed through :func:`redact_paths` on the way out so
    server filesystem paths can never leak, regardless of caller.

    Args:
        status_code: HTTP status code.
        code: Machine-readable error code.
        message: Human-readable error detail.

    Returns:
        A JSON response with an ``{"error": {"code", "message"}}`` body.
    """
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": redact_paths(message)}},
    )


# Multipart framing (boundaries + part headers) allowed on top of the
# configured file-size limit when pre-checking Content-Length.
_MULTIPART_OVERHEAD = 64 * 1024


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before they are buffered.

    The upload service enforces ``max_upload_mb`` on file *content*,
    but by then the multipart parser has already spooled the whole body
    to a temporary file — an unbounded-body DoS. This middleware stops
    the request earlier:

    - a declared ``Content-Length`` above the threshold is refused
      without reading any body bytes;
    - chunked or lying requests are counted as they arrive and aborted
      the moment the threshold is crossed (``FileTooLargeError``
      surfaces from inside request handling, where the domain-error
      handler renders it as 413 ``FILE_TOO_LARGE``).

    Args:
        app: The ASGI app to wrap.
        max_bytes: Configured maximum upload size in bytes.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.threshold = max_bytes + _MULTIPART_OVERHEAD

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Apply the size gate (see class docstring).

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", ()):
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None  # unreadable → byte counting rules
                if declared is not None and declared > self.threshold:
                    await self._reject(scope, send)
                    return
                break

        received = 0
        threshold = self.threshold

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > threshold:
                    # Raised during body parsing, where FastAPI
                    # re-raises HTTPExceptions unchanged (its generic
                    # "error parsing the body" 400 would otherwise
                    # mask this); the app's exception handler renders
                    # the standard 413 envelope.
                    error = FileTooLargeError(self.max_bytes)
                    raise StarletteHTTPException(
                        status_code=413, detail=str(error)
                    )
            return message

        await self.app(scope, limited_receive, send)

    async def _reject(self, scope: Scope, send: Send) -> None:
        """Answer 413 directly, without touching the request body.

        Middleware sits *outside* the exception-handling stack, so the
        envelope is built here rather than raised.

        Args:
            scope: The ASGI scope (for method/path logging).
            send: The ASGI send callable.
        """
        error = FileTooLargeError(self.max_bytes)
        response = _error_response(413, error.code, str(error))
        logger.info(
            "Rejected oversized body for %s %s (content-length > %d bytes)",
            scope.get("method", "?"),
            scope.get("path", "?"),
            self.threshold,
        )
        await response(scope, receive=_discard, send=send)


async def _discard() -> Message:
    """Return a no-op receive for responses that never read a body."""
    return {"type": "http.disconnect"}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run startup and shutdown hooks for the application.

    Args:
        app: The FastAPI application being served.
    """
    settings: Settings = app.state.settings
    logger.info(
        "Starting %s v%s (environment=%s, debug=%s)",
        settings.app_name,
        __version__,
        settings.environment,
        settings.debug,
    )
    # Idempotent schema creation so the service is ready to persist
    # file metadata as soon as it starts.
    init_db()
    yield
    logger.info("Shutting down %s", settings.app_name)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        settings: Optional settings instance; loads from the environment
            when omitted. Injectable for tests.

    Returns:
        The configured FastAPI application.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # Fail fast (and clearly) if the geospatial stack is unusable: the
    # service cannot honour its contract without it. Raises
    # GeospatialDependencyError with a full, actionable report.
    verify_geospatial_environment()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        # Traceback/debug pages are never served, even when
        # ``settings.debug`` is on: every failure stays inside the JSON
        # envelope. Debug only ever affects logging, not responses.
        debug=False,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Body-size gate sits in front of everything: oversized requests
    # are refused before the multipart parser can buffer/spool them.
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_upload_mb * 1024 * 1024,
    )

    # Domain errors → one mapping. Status and code come from the
    # exception itself (``app.exceptions``), so a new domain error only
    # needs to declare them there. Routes never craft errors themselves.
    @app.exception_handler(GeoAppError)
    async def _domain_error(request: Request, exc: GeoAppError) -> JSONResponse:
        # Expected client errors log at INFO; server-side domain
        # failures (5xx) escalate to WARNING so operators notice.
        logger.log(
            logging.WARNING if exc.status_code >= 500 else logging.INFO,
            "%s %s -> %s %s: %s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.code,
            exc,
        )
        return _error_response(exc.status_code, exc.code, str(exc))

    # Malformed requests (bad UUIDs, missing file part, wrong types) →
    # the same envelope instead of FastAPI's default ``detail`` list.
    # Only field locations and validation messages are echoed — never
    # the submitted values or any exception internals.
    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: "
            f"{error.get('msg', 'invalid value')}"
            for error in exc.errors()
        ]
        return _error_response(
            422, "VALIDATION_ERROR", "; ".join(details) or "Invalid request"
        )

    # Framework routing errors (404 unknown route, 405, …) → envelope.
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        message = detail if isinstance(detail, str) else "Request failed"
        code = _HTTP_CODE_BY_STATUS.get(exc.status_code, "HTTP_ERROR")
        response = _error_response(exc.status_code, code, message)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    # Catch-all: anything unexpected. Full traceback goes to the log
    # (and to the server, which re-raises for its own logging); the
    # client gets a generic envelope with no internals.
    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled exception for %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return _error_response(500, "INTERNAL_ERROR", _GENERIC_SERVER_ERROR)

    # Health is mounted unversioned at the root; business routes live
    # under `settings.api_prefix` (e.g. POST /api/files/).
    app.include_router(health_router)
    app.include_router(files_router, prefix=settings.api_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        """Return basic service metadata for unauthenticated callers."""
        return {
            "name": settings.app_name,
            "version": __version__,
            "health": "/health",
        }

    return app


app = create_app()
