"""Logs a service sends to the Admin Panel, by way of its server's Redis.

A service never talks to the panel about a log. It appends one record to a
stream in the server's Admin Panel Redis, and the server's agent collects the
stream and delivers it in batches (see `agent.py`). So a log costs the request
nothing it can feel: the record is written after the response has gone out, the
write gives up after a quarter of a second, and a Redis that is down loses the
log rather than failing the request.

Only what went wrong is logged. A request is recorded when it answers 4xx or
5xx, or raises; the code can add its own records with `emit_log`.

    from cloudon_admin_integration import wire_logging, log_step, emit_log

    wire_logging(app)

    @app.post("/orders/{order_id}")
    async def create_order(order_id: int):
        log_step("reading ERP")       # a failure from here on says so
        ...
        await emit_log("WARNING", "ERP answered slowly", step="reading ERP")

The service is identified by optional environment variables, which the panel
uses to link each record to its inventory:

    CLOUDON_PROJECT_ID   the project's id in the panel
    CLOUDON_DOCKER_ID    the container's id in the panel
    CLOUDON_SERVICE      a name for the service (default: the container name)
    CLOUDON_CONTAINER    the container name (default: $HOSTNAME)

The stream is `cloudon:logs` and is capped at CLOUDON_LOG_STREAM_MAXLEN records
(default 50000), oldest dropped first, so an agent that stops cannot fill the
server's memory.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
import traceback as traceback_module
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis

from cloudon_admin_integration.config import settings

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_LOG_STREAM = "cloudon:logs"
DEFAULT_LOG_STREAM_MAXLEN = 50_000
REQUEST_ID_HEADER = "x-request-id"

#: How long a record may take to write before it is given up on.
WRITE_TIMEOUT_SECONDS = 0.25
#: How much of a failed response's body is kept with its record.
RESPONSE_BODY_LIMIT = 4096
TRACEBACK_LIMIT = 8000

#: The levels the panel files records under.
LEVELS = ("ERROR", "BUG", "WARNING", "INFO", "SECURITY", "PERFORMANCE", "AUDIT")


def _env(name: str) -> str | None:
    return (os.getenv(name) or "").strip() or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class LogSettings:
    stream: str
    maxlen: int
    project_id: str | None
    docker_id: str | None
    service: str
    container: str | None
    module: str | None

    @classmethod
    def from_env(cls) -> "LogSettings":
        container = _env("CLOUDON_CONTAINER") or _env("HOSTNAME")
        return cls(
            stream=_env("CLOUDON_LOG_STREAM") or DEFAULT_LOG_STREAM,
            maxlen=_int_or_none(_env("CLOUDON_LOG_STREAM_MAXLEN")) or DEFAULT_LOG_STREAM_MAXLEN,
            project_id=_env("CLOUDON_PROJECT_ID"),
            docker_id=_env("CLOUDON_DOCKER_ID"),
            service=_env("CLOUDON_SERVICE") or container or "unknown",
            container=container,
            # Only a module the service declared; the package's own default
            # would label every record with a module it may not serve.
            module=_env("APP_MODULE_CODE"),
        )


log_settings = LogSettings.from_env()

# What the current request has said about itself: company, branch, module, the
# step it reached. A mutable dict, so a sync endpoint running in a worker thread
# (which sees a copy of the context) still updates the request's own.
_request_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "cloudon_log_context", default=None
)


def log_step(step: str) -> None:
    """Note how far the current request has got.

    A failure after this is recorded with it as its step, which is what tells a
    timeout talking to the ERP from one parsing what came back.
    """
    context = _request_context.get()
    if context is not None:
        context["step"] = str(step)


def log_context(**values: Any) -> None:
    """Attach company_id, branch_code, module or anything else to the current request's records.

    Known keys go into the record's own fields; anything else lands in `extra`.
    Requests that pass an entitlement check get company and branch without this.
    """
    context = _request_context.get()
    if context is None:
        return
    for key, value in values.items():
        if value is not None:
            context[key] = value


def current_request_id() -> str | None:
    context = _request_context.get()
    return context.get("request_id") if context else None


class _Writer:
    """The service's one connection to the stream, opened on first use."""

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None
        self._warned_at = 0.0
        self._pending: set[asyncio.Task] = set()

    def _client(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                password=settings.redis_password,
                socket_timeout=WRITE_TIMEOUT_SECONDS,
                socket_connect_timeout=WRITE_TIMEOUT_SECONDS,
            )
        return self._redis

    async def write(self, record: dict[str, Any]) -> bool:
        try:
            payload = json.dumps(record, default=str, ensure_ascii=False)
            await asyncio.wait_for(
                self._client().xadd(
                    log_settings.stream,
                    {"record": payload},
                    maxlen=log_settings.maxlen,
                    approximate=True,
                ),
                timeout=WRITE_TIMEOUT_SECONDS,
            )
            return True
        except Exception as exc:  # a log must never be why a request fails
            now = time.monotonic()
            if now - self._warned_at > 60:
                self._warned_at = now
                logger.warning("Could not write a log record to %s: %s", log_settings.stream, exc)
            return False

    def write_later(self, record: dict[str, Any]) -> None:
        """Write without waiting; the task is kept so it is not collected mid-write."""
        try:
            task = asyncio.get_running_loop().create_task(self.write(record))
        except RuntimeError:
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def close(self) -> None:
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=1)
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


_writer = _Writer()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_RECORD_CONTEXT_KEYS = ("company_id", "branch_code", "module", "request_id", "step", "process")


def _base_record(kind: str, context: dict[str, Any] | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "log_id": str(uuid.uuid4()),
        "ts": _now_iso(),
        "service": log_settings.service,
        "kind": kind,
        "project_id": log_settings.project_id,
        "docker_id": log_settings.docker_id,
        "container": log_settings.container or "",
        "module": log_settings.module or "",
    }
    extra: dict[str, Any] = {}
    for key, value in (context or {}).items():
        if key in _RECORD_CONTEXT_KEYS:
            record[key] = value
        elif not key.startswith("_"):
            extra[key] = value
    if "company_id" in record:
        record["company_id"] = str(record["company_id"])
    if "branch_code" in record:
        record["branch_code"] = _int_or_none(record["branch_code"])
    if extra:
        record["extra"] = extra
    return record


def _error_of(exc: BaseException) -> dict[str, str]:
    text = "".join(traceback_module.format_exception(type(exc), exc, exc.__traceback__))
    return {"type": type(exc).__name__, "message": str(exc)[:2000], "traceback": text[-TRACEBACK_LIMIT:]}


async def emit_log(
    level: str,
    title: str,
    message: str | None = None,
    *,
    severity: str | None = None,
    step: str | None = None,
    process: str | None = None,
    error: BaseException | None = None,
    extra: dict[str, Any] | None = None,
    wait: bool = False,
) -> None:
    """Record something the service's own code noticed.

    `level` is one of LEVELS. The record carries the current request's id,
    company and step when there is one. It is written in the background unless
    `wait` is set, and is lost — quietly — if Redis cannot take it.
    """
    level = (level or "INFO").upper()
    if level not in LEVELS:
        level = "INFO"
    record = _base_record("event", _request_context.get())
    record["level"] = level
    record["title"] = str(title)[:255]
    if message:
        record["message"] = str(message)
    if severity:
        record["severity"] = severity.upper()
    if step:
        record["step"] = step
    if process:
        record["process"] = process
    if error is not None:
        record["error"] = _error_of(error)
    if extra:
        record["extra"] = {**record.get("extra", {}), **extra}
    if wait:
        await _writer.write(record)
    else:
        _writer.write_later(record)


class _RouteTemplates:
    """The route a request matched, as written (`/orders/{order_id}`), not as called.

    Starlette records the endpoint it dispatched to, not the route; the route is
    found from the app's routes once per endpoint and remembered.
    """

    def __init__(self) -> None:
        self._by_endpoint: dict[Any, str | None] = {}

    def of(self, scope: dict[str, Any]) -> str | None:
        route = scope.get("route")
        if route is not None and getattr(route, "path", None):
            return route.path
        endpoint = scope.get("endpoint")
        if endpoint is None:
            return None
        if endpoint not in self._by_endpoint:
            app = scope.get("app")
            self._by_endpoint[endpoint] = self._find(getattr(app, "routes", ()) or (), endpoint)
        return self._by_endpoint[endpoint]

    @staticmethod
    def _find(routes: Iterable[Any], endpoint: Any) -> str | None:
        for route in routes:
            if getattr(route, "endpoint", None) is endpoint:
                return getattr(route, "path", None)
        return None


class LoggingMiddleware:
    """Records each failed request to the stream, after its response has gone out.

    Plain ASGI rather than BaseHTTPMiddleware, so it neither buffers responses
    nor moves the endpoint into another task, and it sees an exception before
    any envelope has turned it into a 500.
    """

    def __init__(
        self,
        app,
        *,
        excluded_paths: Iterable[str] = (),
        skip_statuses: Iterable[int] = (),
    ) -> None:
        self.app = app
        self.excluded_paths = tuple(excluded_paths)
        self.skip_statuses = frozenset(skip_statuses)
        self.routes = _RouteTemplates()

    def _excluded(self, path: str) -> bool:
        return any(path == item or path.startswith(item.rstrip("/") + "/") for item in self.excluded_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self._excluded(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or ())
        request_id = (headers.get(REQUEST_ID_HEADER.encode()) or b"").decode("latin-1").strip()[:120]
        request_id = request_id or uuid.uuid4().hex
        context: dict[str, Any] = {"request_id": request_id}
        company_id = (headers.get(b"x-company-id") or b"").decode("latin-1").strip()
        if company_id:
            context["company_id"] = company_id
        branch_code = _int_or_none((headers.get(b"x-branch-code") or b"").decode("latin-1").strip())
        if branch_code is not None:
            context["branch_code"] = branch_code
        token = _request_context.set(context)

        started = time.perf_counter()
        status: int | None = None
        body = bytearray()

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                if not any(name.lower() == REQUEST_ID_HEADER.encode() for name, _ in message["headers"]):
                    message["headers"] = [*message["headers"], (REQUEST_ID_HEADER.encode(), request_id.encode())]
            elif message["type"] == "http.response.body" and status and status >= 400:
                if len(body) < RESPONSE_BODY_LIMIT:
                    body.extend(message.get("body", b"")[: RESPONSE_BODY_LIMIT - len(body)])
            await send(message)

        error: BaseException | None = None
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            error = exc
            raise
        finally:
            _request_context.reset(token)
            if error is not None or (status is not None and status >= 400 and status not in self.skip_statuses):
                try:
                    record = self._record(scope, context, status, started, bytes(body), error)
                    _writer.write_later(record)
                except Exception:  # never let the log break the request
                    logger.debug("Could not build a log record", exc_info=True)

    def _record(self, scope, context, status, started, body, error) -> dict[str, Any]:
        record = _base_record("request", context)
        route = self.routes.of(scope)
        record.update(
            {
                "method": scope.get("method", ""),
                "path": scope.get("path", ""),
                "route": route or "",
                "status": status if error is None else (status or 500),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        )
        if error is not None:
            record["error"] = _error_of(error)
        if body:
            text = body.decode("utf-8", errors="replace")
            try:
                record["response"] = json.loads(text)
            except ValueError:
                record["response"] = text
        return record


def wire_logging(
    app,
    *,
    excluded_paths: Iterable[str] | None = None,
    skip_statuses: Iterable[int] = (),
) -> None:
    """Send the app's failed requests to the Admin Panel.

    Opt-in, and separate from wire_integration, so a service rebuilt onto a
    newer package does not start logging until it asks to. Wire it last, so it
    wraps everything else and sees exceptions before they become 500s.
    `skip_statuses` leaves out statuses that are routine for this service
    (say 404 on an API scanners like to probe).
    """
    app.add_middleware(
        LoggingMiddleware,
        excluded_paths=tuple(excluded_paths if excluded_paths is not None else settings.integration_excluded_paths),
        skip_statuses=tuple(skip_statuses),
    )

    @app.on_event("shutdown")
    async def _close_log_writer() -> None:
        await _writer.close()
