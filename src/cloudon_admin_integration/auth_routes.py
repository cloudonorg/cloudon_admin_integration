from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cloudon_admin_integration.cache import IntegrationCache
from cloudon_admin_integration.dependencies import bootstrap_and_cache_client, get_cache

auth_router = APIRouter(tags=["Integration Auth"])


class AuthTokenRequest(BaseModel):
    client_id: str
    client_secret: str
    branch_code: str | None = None
    module_code: str | None = None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


@auth_router.post("/auth/token")
@auth_router.post("/auth/token/")
async def auth_token(
    payload: AuthTokenRequest,
    cache: IntegrationCache = Depends(get_cache),
) -> dict[str, Any]:
    client_id = _clean(payload.client_id)
    client_secret = _clean(payload.client_secret)
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=422,
            detail={"reason": "client_credentials_missing", "message": "client_id and client_secret are required"},
        )
    try:
        return await bootstrap_and_cache_client(
            client_id,
            client_secret,
            branch_code=_clean(payload.branch_code),
            module_code=_clean(payload.module_code),
            cache=cache,
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json()
        except Exception:
            pass
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail={"reason": "admin_panel_unavailable", "message": str(exc)}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"reason": "cache_unavailable", "message": str(exc)}) from exc
