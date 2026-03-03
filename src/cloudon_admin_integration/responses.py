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


def normalize_response_payload(payload: Any, status_code: int) -> dict[str, Any]:
    if isinstance(payload, dict):
        if "success" in payload:
            success = bool(payload.get("success"))
            error = payload.get("error")
            if not success and error is None:
                if "detail" in payload:
                    error = payload.get("detail")
                elif "error_code" in payload:
                    error = {"code": payload.get("error_code")}
                elif payload.get("message"):
                    error = payload.get("message")
            return response_envelope(
                success=success,
                error=error,
                message=payload.get("message"),
                data=payload.get("data"),
            )

        if status_code >= 400:
            return response_envelope(
                success=False,
                error=payload.get("detail") or payload,
                message=payload.get("message"),
                data=payload.get("data"),
            )

    if status_code >= 400:
        return response_envelope(success=False, error=payload, message="Request failed", data=None)

    return response_envelope(success=True, error=None, message=None, data=payload)


def wire_response_envelope(app: FastAPI, *, excluded_paths: set[str] | None = None) -> None:
    excluded = excluded_paths or {"/docs", "/redoc", "/openapi.json", "/favicon.ico"}

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):  # noqa: ARG001
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=response_envelope(success=False, error=exc.detail, message=message, data=None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):  # noqa: ARG001
        return JSONResponse(
            status_code=422,
            content=response_envelope(
                success=False,
                error=exc.errors(),
                message="Validation error",
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
                error={"type": exc.__class__.__name__},
                message="Internal server error",
                data=None,
            ),
        )

    @app.middleware("http")
    async def _response_envelope_middleware(request: Request, call_next):
        response = await call_next(request)

        if request.url.path in excluded:
            return response

        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" not in content_type:
            return response

        body_bytes = b""
        async for chunk in response.body_iterator:
            body_bytes += chunk

        if not body_bytes:
            normalized = normalize_response_payload(None, response.status_code)
        else:
            try:
                payload = json.loads(body_bytes)
            except JSONDecodeError:
                return JSONResponse(
                    status_code=response.status_code,
                    content=response_envelope(
                        success=response.status_code < 400,
                        error=None if response.status_code < 400 else "Non-JSON response body",
                        message=None if response.status_code < 400 else "Request failed",
                        data=body_bytes.decode("utf-8", errors="replace"),
                    ),
                )
            normalized = normalize_response_payload(payload, response.status_code)

        return JSONResponse(status_code=response.status_code, content=normalized)
