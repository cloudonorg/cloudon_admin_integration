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


def _extract_error(detail: Any, fallback_error: str = "Request failed") -> str:
    if isinstance(detail, dict):
        reason = detail.get("reason") or detail.get("error") or detail.get("message")
        text = str(reason or fallback_error).replace("_", " ").strip()
        return text[:1].upper() + text[1:] if text else fallback_error
    if isinstance(detail, str):
        text = detail.replace("_", " ").strip()
        return text[:1].upper() + text[1:] if text else fallback_error
    return fallback_error


def normalize_response_payload(payload: Any, status_code: int, *, default_message: str | None = None) -> dict[str, Any]:
    if isinstance(payload, dict):
        if "success" in payload:
            success = bool(payload.get("success"))
            error = payload.get("error")
            message = payload.get("message")
            if success and not message:
                message = default_message
            if not success:
                if error is None:
                    if "detail" in payload:
                        error = _extract_error(payload.get("detail"))
                    elif "error_code" in payload:
                        error = _extract_error(payload.get("error_code"))
                    elif payload.get("message"):
                        error = _extract_error(payload.get("message"))
                    else:
                        error = _extract_error(payload)
                message = None
                data = None
            else:
                data = payload.get("data")
            return response_envelope(
                success=success,
                error=error,
                message=message,
                data=data,
            )

        if status_code >= 400:
            err = _extract_error(payload.get("detail") or payload.get("error") or payload.get("message") or payload)
            return response_envelope(
                success=False,
                error=err,
                message=None,
                data=None,
            )

    if status_code >= 400:
        return response_envelope(success=False, error=_extract_error(payload), message=None, data=None)

    return response_envelope(success=True, error=None, message=default_message, data=payload)


def wire_response_envelope(app: FastAPI, *, excluded_paths: set[str] | None = None) -> None:
    excluded = excluded_paths or {"/docs", "/redoc", "/openapi.json", "/favicon.ico"}

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):  # noqa: ARG001
        return JSONResponse(
            status_code=exc.status_code,
            content=response_envelope(success=False, error=_extract_error(exc.detail), message=None, data=None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):  # noqa: ARG001
        return JSONResponse(
            status_code=422,
            content=response_envelope(
                success=False,
                error="validation_error",
                message=None,
                data=None,
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
                message=None,
                data=None,
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
                        message=default_message if response.status_code < 400 else None,
                        data=body_bytes.decode("utf-8", errors="replace") if response.status_code < 400 else None,
                    ),
                )
            normalized = normalize_response_payload(payload, response.status_code, default_message=default_message)

        return JSONResponse(status_code=response.status_code, content=normalized)
