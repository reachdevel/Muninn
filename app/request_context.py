"""Pure-ASGI request-id, timing and error-envelope middleware.

Why not ``@app.middleware("http")``: Starlette's ``BaseHTTPMiddleware`` sits
between the endpoint and the server's ``receive`` channel and re-plumbs it
through its own task machinery, which swallows ``http.disconnect``. That is not
cosmetic - ``Request.is_disconnected()`` then *never* returns True, so
``/search`` could never tell that a caller had hung up and every abandoned
request went on occupying a worker until its deadline. Measured on this service:
with the middleware, five socket-level hang-ups produced zero disconnects
detected over ~50 polls each; without it, the second poll reports the
disconnect.

This middleware is a plain ASGI callable instead, so ``receive`` reaches the
endpoint exactly as the server sent it. That is the whole difference, and it is
the only reason this file exists.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("muninn")

#: Probe endpoints: a healthcheck polls these every few seconds and they must
#: not inflate the service's own request counters or latency histograms.
UNMEASURED_PATHS = frozenset({"/health/live", "/metrics"})

Scope = Scope  # re-exported name kept for the annotations above


class RequestContextMiddleware:
    """Attach a request id, time the request, and return a JSON 500 envelope.

    Unhandled exceptions get a JSON body with the request id instead of
    Starlette's plain-text 500 and stack trace. The id is echoed in the response
    and in the log record, so an operator can find the traceback without
    exposing internals to the caller. Errors raised deliberately
    (``HTTPException`` and the explicit ``JSONResponse`` bodies) are unaffected:
    their documented shapes still stand.
    """

    def __init__(self, app: ASGIApp, record: Callable[[str, int, float], None]) -> None:
        self.app = app
        self._record = record

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - lifespan/websocket
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        request_id = _header(headers, b"x-request-id") or uuid.uuid4().hex[:12]
        # Starlette's Request.state reads scope["state"], so this is what
        # endpoints and log records see as request.state.request_id.
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id
        path = scope.get("path", "")
        measured = path not in UNMEASURED_PATHS
        started = time.perf_counter()
        status_holder = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_holder
            if message["type"] == "http.response.start":
                status_holder = message["status"]
                raw_headers = message.setdefault("headers", [])
                raw_headers.append((b"x-request-id", request_id.encode("ascii")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception(
                "unhandled error [request_id=%s] %s %s", request_id, scope.get("method"), path
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "detail": "an unexpected error occurred",
                    "request_id": request_id,
                },
            )
            response.headers["X-Request-Id"] = request_id
            await response(scope, receive, send)
        finally:
            if measured:
                self._record(path, status_holder, time.perf_counter() - started)


def _header(headers: dict[bytes, bytes], name: bytes) -> str | None:
    value = headers.get(name)
    return value.decode("latin-1") if value else None


def install(app: Any, record: Callable[[str, int, float], None]) -> None:
    """Attach the middleware to a FastAPI app.

    ``add_middleware`` (rather than the decorator) keeps this in the ASGI stack,
    ahead of the router, which is what preserves ``receive``.
    """
    app.add_middleware(RequestContextMiddleware, record=record)