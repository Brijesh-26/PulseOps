"""ASGI middleware: correlation IDs, request size limits, access logs and metrics."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pulseops.observability.logging import (
    get_logger,
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)
from pulseops.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

log = get_logger("pulseops.http")

_CID_RE = re.compile(r"^[A-Za-z0-9\-_.]{8,64}$")
CORRELATION_HEADER = "x-correlation-id"


def _error_body(code: str, message: str, cid: str | None) -> bytes:
    return json.dumps(
        {"error": {"code": code, "message": message, "details": None, "correlation_id": cid}}
    ).encode()


class CorrelationIdMiddleware:
    """Accepts a well-formed inbound correlation ID or generates one, and echoes it back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        inbound = None
        for key, value in scope.get("headers", []):
            if key.decode().lower() == CORRELATION_HEADER:
                inbound = value.decode()
                break
        cid = inbound if inbound and _CID_RE.match(inbound) else new_correlation_id()
        scope.setdefault("state", {})["correlation_id"] = cid
        token = set_correlation_id(cid)
        start = time.perf_counter()
        status_holder: dict[str, int] = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((CORRELATION_HEADER.encode(), cid.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = _route_template(scope)
            method = scope.get("method", "")
            HTTP_REQUESTS.labels(method, route, str(status_holder["status"])).inc()
            HTTP_LATENCY.labels(method, route).observe(elapsed)
            if route not in ("/health", "/metrics"):
                log.info(
                    "http_request",
                    method=method,
                    route=route,
                    status=status_holder["status"],
                    duration_ms=round(elapsed * 1000, 1),
                )
            reset_correlation_id(token)


def _route_template(scope: Scope) -> str:
    route: Any = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class BodySizeLimitMiddleware:
    """Rejects request bodies over ``max_bytes`` (checks Content-Length and streamed size)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    too_big = int(value) > self.max_bytes
                except ValueError:
                    too_big = True
                if too_big:
                    await self._reject(scope, send)
                    return

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise _BodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLargeError:
            if exceeded:
                await self._reject(scope, send)

    async def _reject(self, scope: Scope, send: Send) -> None:
        cid = scope.get("state", {}).get("correlation_id")
        body = _error_body("payload_too_large", f"Request body exceeds {self.max_bytes} bytes", cid)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _BodyTooLargeError(Exception):
    pass
