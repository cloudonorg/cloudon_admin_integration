from typing import Any

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


async def require_valid_api_client_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> ApiClientClaims:
    """Resolve a bearer token to the client it was minted for.

    The token is opaque and means nothing outside this service: its scope lives
    in Redis under a digest of it, put there by /auth/token after checking the
    caller's secret against the credential the panel cached. Nothing is signed,
    so a stolen token is useless once its entry expires or the credential is
    withdrawn — and there is no verification key to distribute or rotate.
    """
    if credentials is None:
        _fail(401, "token_missing", "Missing bearer token")

    if credentials.scheme.lower() != "bearer":
        _fail(401, "token_scheme_invalid", "Authorization scheme must be Bearer")

    from cloudon_admin_integration.client_auth import session_expired, token_digest
    from cloudon_admin_integration.dependencies import get_cache

    try:
        session = await get_cache().get_access_token(token_digest(credentials.credentials))
    except RuntimeError as exc:
        _fail(503, "cache_unavailable", str(exc))

    if not session:
        _fail(401, "token_invalid", "Invalid token")
    if session_expired(session):
        _fail(401, "token_expired", "Token expired")

    company_code = _to_int_or_none(session.get("company_code"))
    if company_code is None:
        _fail(401, "token_company_missing", "Token missing company_code")

    module_code = (session.get("module_code") or "").strip() or None
    allowed_module_codes = _allowed_module_codes()
    if (
        settings.enforce_token_module_match
        and module_code
        and allowed_module_codes
        and module_code not in allowed_module_codes
        and module_code != "*"
    ):
        _fail(403, "token_module_mismatch", "Token module_code does not match this middleware module set")

    return ApiClientClaims(
        token_type="api_client",
        client_id=session.get("client_id"),
        company_id=(str(session.get("company_id")).strip() if session.get("company_id") is not None else None),
        company_code=company_code,
        company_name=(session.get("company_name") or None),
        infrastructure_id=(session.get("infrastructure_id") or None),
        infrastructure_serial_num=(session.get("infrastructure_serial_num") or None),
        infrastructure_domain=(session.get("infrastructure_domain") or None),
        branch_code=_to_int_or_none(session.get("branch_code")),
        module_code=module_code,
        raw=session,
    )
