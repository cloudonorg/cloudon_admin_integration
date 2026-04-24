from typing import Any

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from cloudon_admin_integration.config import settings


bearer_scheme = HTTPBearer(auto_error=False)


class ApiClientClaims(BaseModel):
    token_type: str
    client_id: str | None = None
    company_id: str | None = None
    company_code: int | None = None
    company_name: str | None = None
    infrastructure_id: str | None = None
    infrastructure_serial_num: str | None = None
    infrastructure_domain: str | None = None
    branch_code: int | None = None
    module_code: str | None = None
    iat: int | None = None
    exp: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


def _fail(status_code: int, reason: str, message: str) -> None:
    raise HTTPException(status_code=status_code, detail={"reason": reason, "message": message})


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _allowed_module_codes() -> set[str]:
    codes = settings.app_module_codes or (settings.app_module_code,)
    return {code for code in codes if code}


def _peek_unverified_claims(token: str) -> dict[str, Any] | None:
    try:
        decoded = jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
            algorithms=[settings.admin_panel_jwt_algorithm],
        )
    except jwt.InvalidTokenError:
        return None
    return decoded if isinstance(decoded, dict) else None


async def _resolve_verification_key(token: str) -> str:
    unverified = _peek_unverified_claims(token) or {}
    client_id = (unverified.get("client_id") or "").strip() or None
    cache_error: RuntimeError | None = None
    algorithm = settings.admin_panel_jwt_algorithm.upper()

    if client_id:
        try:
            from cloudon_admin_integration.dependencies import get_cache
            session = await get_cache().get_client_session(client_id)
        except RuntimeError as exc:
            cache_error = exc
        else:
            if isinstance(session, dict):
                verification_key = (session.get("verification_key") or "").strip()
                if verification_key:
                    return verification_key

                if algorithm.startswith("HS"):
                    client_secret = (session.get("client_secret") or "").strip()
                    if client_secret:
                        return client_secret

    try:
        return settings.jwt_verification_key()
    except RuntimeError as exc:
        if cache_error is not None:
            _fail(503, "cache_unavailable", str(cache_error))
        raise exc


async def require_valid_api_client_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> ApiClientClaims:
    if credentials is None:
        _fail(401, "token_missing", "Missing bearer token")

    if credentials.scheme.lower() != "bearer":
        _fail(401, "token_scheme_invalid", "Authorization scheme must be Bearer")

    token = credentials.credentials
    try:
        verification_key = await _resolve_verification_key(token)
    except RuntimeError as exc:
        _fail(500, "token_verification_unavailable", str(exc))

    try:
        decoded = jwt.decode(
            token,
            verification_key,
            algorithms=[settings.admin_panel_jwt_algorithm],
            audience=settings.admin_panel_jwt_audience,
            options={"verify_aud": bool(settings.admin_panel_jwt_audience)},
        )
    except jwt.ExpiredSignatureError as exc:
        _fail(401, "token_expired", "Token expired")
    except jwt.InvalidTokenError as exc:
        _fail(401, "token_invalid", "Invalid token")
    except RuntimeError as exc:
        _fail(500, "token_verification_unavailable", str(exc))

    token_type = decoded.get("token_type")
    if token_type != "api_client":
        _fail(401, "token_type_invalid", "Invalid token_type")

    company_code = _to_int_or_none(decoded.get("company_code"))
    if company_code is None:
        _fail(401, "token_company_missing", "Token missing company_code")

    token_module_code = (decoded.get("module_code") or "").strip() or None
    allowed_module_codes = _allowed_module_codes()
    if (
        settings.enforce_token_module_match
        and token_module_code
        and token_module_code not in allowed_module_codes
        and token_module_code != "*"
    ):
        _fail(403, "token_module_mismatch", "Token module_code does not match this middleware module set")

    return ApiClientClaims(
        token_type=token_type,
        client_id=decoded.get("client_id"),
        company_id=(str(decoded.get("company_id")).strip() if decoded.get("company_id") is not None else None),
        company_code=company_code,
        company_name=(decoded.get("company_name") or None),
        infrastructure_id=(decoded.get("infrastructure_id") or None),
        infrastructure_serial_num=(decoded.get("infrastructure_serial_num") or None),
        infrastructure_domain=(decoded.get("infrastructure_domain") or None),
        branch_code=_to_int_or_none(decoded.get("branch_code")),
        module_code=token_module_code,
        iat=decoded.get("iat"),
        exp=decoded.get("exp"),
        raw=decoded,
    )
