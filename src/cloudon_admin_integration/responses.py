import json
import logging
from json import JSONDecodeError
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)


def response_envelope(*, success: bool, data: Any = None, message: str | None = None, error: Any = None) -> dict[str, Any]:
    return {
        "success": success,
        "error": error,
        "message": message,
        "data": data,
    }


def _extract_error_and_message(detail: Any, fallback_message: str = "Request failed") -> tuple[Any, str]:
    if isinstance(detail, dict):
        reason = detail.get("reason")
        message = detail.get("message") or fallback_message
        # Keep external error payload human-friendly.
        return (message or reason or fallback_message), message
    if isinstance(detail, str):
        return detail, detail
    return fallback_message, fallback_message


def normalize_response_payload(payload: Any, status_code: int, *, default_message: str | None = None) -> dict[str, Any]:
    def _error_data(value: Any) -> Any:
        return value if value is not None else []

    if isinstance(payload, dict):
        if "success" in payload:
            success = bool(payload.get("success"))
            error = payload.get("error")
            message = payload.get("message")
            if not success and error is None:
                if "detail" in payload:
                    error, message = _extract_error_and_message(payload.get("detail"))
                elif "error_code" in payload:
                    error = payload.get("error_code")
                    message = message or "Request failed"
                elif payload.get("message"):
                    error = payload.get("message")
            if success and not message:
                message = default_message
            data = payload.get("data")
            if not success and data is None:
                data = []
            return response_envelope(
                success=success,
                error=error,
                message=message,
                data=data,
            )

        if status_code >= 400:
            err, msg = _extract_error_and_message(payload.get("detail") or payload)
            return response_envelope(
                success=False,
                error=err,
                message=msg,
                data=_error_data(payload.get("data")),
            )

    if status_code >= 400:
        return response_envelope(success=False, error=payload, message="Request failed", data=[])

    return response_envelope(success=True, error=None, message=default_message, data=payload)


def wire_response_envelope(app: FastAPI, *, excluded_paths: set[str] | None = None) -> None:
    excluded = excluded_paths or {"/docs", "/redoc", "/openapi.json", "/favicon.ico"}

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):  # noqa: ARG001
        error, message = _extract_error_and_message(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=response_envelope(success=False, error=error, message=message, data=[]),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):  # noqa: ARG001
        return JSONResponse(
            status_code=422,
            content=response_envelope(
                success=False,
                error="validation_error",
                message="Validation error",
                data=[],
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error for %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content=response_envelope(
                success=False,
                error="internal_server_error",
                message="Internal server error",
                data=[],
            ),
        )

    @app.middleware("http")
    async def _response_envelope_middleware(request: Request, call_next):
        response = await call_next(request)
        default_message = getattr(request.state, "integration_message", None)

        if request.url.path in excluded:
            return response

        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" not in content_type:
            return response

        body_bytes = b""
        async for chunk in response.body_iterator:
            body_bytes += chunk

        if not body_bytes:
            normalized = normalize_response_payload(None, response.status_code, default_message=default_message)
        else:
            try:
                payload = json.loads(body_bytes)
            except JSONDecodeError:
                return JSONResponse(
                    status_code=response.status_code,
                    content=response_envelope(
                        success=response.status_code < 400,
                        error=None if response.status_code < 400 else "Non-JSON response body",
                        message=default_message if response.status_code < 400 else "Request failed",
                        data=body_bytes.decode("utf-8", errors="replace") if response.status_code < 400 else [],
                    ),
                )
            normalized = normalize_response_payload(payload, response.status_code, default_message=default_message)

        return JSONResponse(status_code=response.status_code, content=normalized)
