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
    branch_code: int | None = None
    module_code: str | None = None
    iat: int | None = None
    exp: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


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


async def require_valid_api_client_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> ApiClientClaims:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authorization scheme must be Bearer")

    token = credentials.credentials
    try:
        decoded = jwt.decode(
            token,
            settings.jwt_verification_key(),
            algorithms=[settings.admin_panel_jwt_algorithm],
            audience=settings.admin_panel_jwt_audience,
            options={"verify_aud": bool(settings.admin_panel_jwt_audience)},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    token_type = decoded.get("token_type")
    if token_type != "api_client":
        raise HTTPException(status_code=401, detail="Invalid token_type")

    company_code = _to_int_or_none(decoded.get("company_code"))
    if company_code is None:
        raise HTTPException(status_code=401, detail="Token missing company_code")

    token_module_code = (decoded.get("module_code") or "").strip() or None
    if (
        settings.enforce_token_module_match
        and token_module_code
        and token_module_code not in {settings.app_module_code, "*"}
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "token_module_mismatch",
                "message": "Token module_code does not match this middleware module",
                "token_module_code": token_module_code,
                "expected_module_code": settings.app_module_code,
            },
        )

    return ApiClientClaims(
        token_type=token_type,
        client_id=decoded.get("client_id"),
        company_id=(str(decoded.get("company_id")).strip() if decoded.get("company_id") is not None else None),
        company_code=company_code,
        branch_code=_to_int_or_none(decoded.get("branch_code")),
        module_code=token_module_code,
        iat=decoded.get("iat"),
        exp=decoded.get("exp"),
        raw=decoded,
    )
