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


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_error_parts(
    detail: Any,
    *,
    fallback_error: str = "request_failed",
    fallback_message: str | None = None,
) -> tuple[str, str | None]:
    if isinstance(detail, dict):
        error = _clean_text(
            detail.get("reason")
            or detail.get("error")
            or detail.get("error_code")
            or detail.get("code")
        )
        message = _clean_text(detail.get("message"))
        nested_detail = detail.get("detail")

        if (error is None or message is None) and nested_detail is not None:
            nested_error, nested_message = _extract_error_parts(
                nested_detail,
                fallback_error=fallback_error,
                fallback_message=fallback_message,
            )
            error = error or nested_error
            message = message or nested_message

        if message is None and nested_detail is not None and not isinstance(nested_detail, (dict, list)):
            message = _clean_text(nested_detail)

        return error or fallback_error, message or fallback_message
    if isinstance(detail, str):
        text = _clean_text(detail)
        return text or fallback_error, text or fallback_message
    return fallback_error, fallback_message


def _extract_error(detail: Any, fallback_error: str = "request_failed") -> str:
    error, _ = _extract_error_parts(detail, fallback_error=fallback_error)
    return error


def _extract_response_error_parts(detail: Any) -> tuple[str, str | None]:
    return _extract_error_parts(detail, fallback_message="Request failed")


def normalize_response_payload(payload: Any, status_code: int, *, default_message: str | None = None) -> dict[str, Any]:
    if isinstance(payload, dict):
        if "success" in payload:
            success = bool(payload.get("success"))
            error = payload.get("error")
            message = payload.get("message")
            if success and not message:
                message = default_message
            if not success:
                detail = payload.get("detail") if "detail" in payload else payload
                extracted_error, extracted_message = _extract_response_error_parts(detail)
                error = error or extracted_error
                message = message or extracted_message
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
            detail = payload.get("detail") if "detail" in payload else payload
            err, msg = _extract_response_error_parts(detail)
            return response_envelope(
                success=False,
                error=err,
                message=msg,
                data=None,
            )

    if status_code >= 400:
        err, msg = _extract_response_error_parts(payload)
        return response_envelope(success=False, error=err, message=msg, data=None)

    return response_envelope(success=True, error=None, message=default_message, data=payload)


def wire_response_envelope(app: FastAPI, *, excluded_paths: set[str] | None = None) -> None:
    excluded = excluded_paths or {"/docs", "/redoc", "/openapi.json", "/favicon.ico"}

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):  # noqa: ARG001
        error, message = _extract_response_error_parts(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=response_envelope(
                success=False,
                error=error,
                message=message,
                data=None,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):  # noqa: ARG001
        return JSONResponse(
            status_code=422,
            content=response_envelope(
                success=False,
                error="validation_error",
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
                error="internal_server_error",
                message="Internal server error",
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
                        error=None if response.status_code < 400 else "non_json_response_body",
                        message=default_message if response.status_code < 400 else "Non-JSON response body",
                        data=body_bytes.decode("utf-8", errors="replace") if response.status_code < 400 else None,
                    ),
                )
            normalized = normalize_response_payload(payload, response.status_code, default_message=default_message)

        return JSONResponse(status_code=response.status_code, content=normalized)
