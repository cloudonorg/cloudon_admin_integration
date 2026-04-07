from collections.abc import Sequence

from fastapi import Depends, FastAPI

from cloudon_admin_integration.config import settings
from cloudon_admin_integration.dependencies import (
    require_module_entitlement_for,
    require_module_entitlements,
    require_module_entitlements_for,
    startup_integration,
    shutdown_integration,
)
from cloudon_admin_integration.sync_routes import sync_router
from cloudon_admin_integration.responses import wire_response_envelope


def wire_integration(
    app: FastAPI,
    *,
    include_sync_routes: bool = True,
    include_response_envelope: bool = True,
) -> None:
    if include_response_envelope and settings.integration_wrap_responses:
        wire_response_envelope(app, excluded_paths=set(settings.integration_excluded_paths))

    @app.on_event("startup")
    async def _integration_startup() -> None:
        await startup_integration()

    @app.on_event("shutdown")
    async def _integration_shutdown() -> None:
        await shutdown_integration()

    if include_sync_routes:
        app.include_router(sync_router)


def entitlement_dependency(module_code: str):
    return Depends(require_module_entitlement_for(module_code))


def entitlements_dependency(module_codes: str | Sequence[str] | None = None):
    if module_codes is None:
        return Depends(require_module_entitlements)
    return Depends(require_module_entitlements_for(module_codes))
