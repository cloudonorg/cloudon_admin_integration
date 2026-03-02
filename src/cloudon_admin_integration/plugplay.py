from fastapi import Depends, FastAPI

from cloudon_admin_integration.dependencies import require_module_entitlement_for
from cloudon_admin_integration.sync_routes import auth_proxy_router, sync_router
from cloudon_admin_integration.dependencies import startup_integration, shutdown_integration


def wire_integration(app: FastAPI, *, include_sync_routes: bool = True, include_auth_proxy: bool = True) -> None:
    @app.on_event("startup")
    async def _integration_startup() -> None:
        await startup_integration()

    @app.on_event("shutdown")
    async def _integration_shutdown() -> None:
        await shutdown_integration()

    if include_sync_routes:
        app.include_router(sync_router)
    if include_auth_proxy:
        app.include_router(auth_proxy_router)


def entitlement_dependency(module_code: str):
    return Depends(require_module_entitlement_for(module_code))
