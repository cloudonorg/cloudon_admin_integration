from collections.abc import Sequence
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from cloudon_admin_integration.config import settings
from cloudon_admin_integration.cache import IntegrationCache
from cloudon_admin_integration.dependencies import (
    bootstrap_and_cache_client,
    EntitlementsContext,
    get_cache,
    require_all_module_entitlements,
    require_module_entitlement_for,
    require_module_entitlements,
    require_module_entitlements_for,
    startup_integration,
    shutdown_integration,
)
from cloudon_admin_integration.sync_routes import sync_router
from cloudon_admin_integration.responses import wire_response_envelope


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


def _register_auth_routes(app: FastAPI) -> None:
    @app.post("/auth/token")
    @app.post("/auth/token/")
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


def _register_admin_routes(app: FastAPI) -> None:
    @app.get("/admin/parameters")
    @app.get("/admin/parameters/")
    async def admin_parameters(
        ctx: EntitlementsContext = Depends(require_all_module_entitlements),
    ) -> list[dict[str, Any]]:
        return ctx.model_dump()


def wire_integration(
    app: FastAPI,
    *,
    include_sync_routes: bool = True,
    include_auth_routes: bool = False,
    include_admin_routes: bool = False,
    include_response_envelope: bool = True,
) -> None:
    """Attach the integration layer to a FastAPI app.

    A bare wire_integration(app) used to publish /auth/token, /admin/parameters
    and the sync router all at once. One consumer stripped them again by rewriting
    app.router.routes; another left them exposed. The two endpoints that are
    incidental now default to off, and publishing them is a deliberate choice:

    include_auth_routes    POST /auth/token, which mints client tokens.
    include_admin_routes   GET /admin/parameters, which returns every entitlement
                           for the calling company.

    include_sync_routes stays on: those are the webhook endpoints the backend
    delivers changes to, they are the reason a service installs this package, and
    they are guarded by the sync key. GET /get-redis-data now refuses an
    unscoped fleet-wide dump unless the caller asks for one explicitly.
    """
    if include_response_envelope and settings.integration_wrap_responses:
        wire_response_envelope(app, excluded_paths=set(settings.integration_excluded_paths))

    @app.on_event("startup")
    async def _integration_startup() -> None:
        await startup_integration()

    @app.on_event("shutdown")
    async def _integration_shutdown() -> None:
        await shutdown_integration()

    if include_auth_routes:
        _register_auth_routes(app)
    if include_admin_routes:
        _register_admin_routes(app)
    if include_sync_routes:
        app.include_router(sync_router)


def entitlement_dependency(module_code: str):
    return Depends(require_module_entitlement_for(module_code))


def entitlements_dependency(module_codes: str | Sequence[str] | None = None):
    if module_codes is None:
        return Depends(require_module_entitlements)
    return Depends(require_module_entitlements_for(module_codes))
