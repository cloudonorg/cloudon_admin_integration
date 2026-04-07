from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from cloudon_admin_integration.cache import IntegrationCache
from cloudon_admin_integration.dependencies import (
    get_cache,
    perform_full_sync,
    require_sync_key,
)

sync_router = APIRouter(tags=["Integration Sync"])


class SingleLicenseSyncPayload(BaseModel):
    operation: Literal["upsert", "delete"]
    module_name: str | None = None
    module_code: str
    serial_num: str | None = None
    company_id: str | None = None
    company_code: int
    company_name: str | None = None
    infrastructure_id: str | None = None
    infrastructure_serial_num: str | None = None
    infrastructure_domain: str
    to_date: str | None = None
    state: str | None = None
    revoked_at: str | None = None


class SingleParamSyncPayload(BaseModel):
    operation: Literal["upsert", "delete"] = "upsert"
    module_code: str
    company_id: str | None = None
    company_code: int
    company_name: str | None = None
    infrastructure_id: str | None = None
    infrastructure_serial_num: str | None = None
    infrastructure_domain: str
    params: dict[str, Any] | None = None
    param_key: str | None = None
    param_value: Any = None


async def _refresh_all(cache: IntegrationCache) -> dict[str, Any]:
    return await perform_full_sync(cache=cache)


@sync_router.post("/sync-single-license", dependencies=[Depends(require_sync_key)])
async def sync_single_license(
    payload: SingleLicenseSyncPayload,
    cache: IntegrationCache = Depends(get_cache),
):
    try:
        result = await _refresh_all(cache)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    return {"status": "ok", "operation": payload.operation, "result": result}


@sync_router.post("/sync-single-param", dependencies=[Depends(require_sync_key)])
async def sync_single_param(
    payload: SingleParamSyncPayload,
    cache: IntegrationCache = Depends(get_cache),
):
    try:
        result = await _refresh_all(cache)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    return {"status": "ok", "operation": payload.operation, "result": result}


class CompanySyncPayload(BaseModel):
    company_code: int | None = None


@sync_router.post("/sync-company-change", dependencies=[Depends(require_sync_key)])
async def sync_company_change(payload: CompanySyncPayload):
    try:
        result = await perform_full_sync()
        return {
            "status": "ok",
            "operation": "company_change_full_sync",
            "requested_company_code": payload.company_code,
            "result": result,
        }
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Admin panel sync failed: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc


@sync_router.post("/sync-redis-data", dependencies=[Depends(require_sync_key)])
async def sync_redis_data():
    try:
        result = await perform_full_sync()
        return {"status": "ok", "operation": "full_sync", "result": result}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Admin panel sync failed: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc


@sync_router.get("/get-redis-data", dependencies=[Depends(require_sync_key)])
async def get_redis_data(
    company_id: str | None = Query(default=None),
    company_code: int | None = Query(default=None),
    module_code: str | None = Query(default=None),
    branch_code: str | None = Query(default=None),
    domain: str | None = Query(default=None),
    refresh: bool = Query(default=False),
    cache: IntegrationCache = Depends(get_cache),
):
    if refresh:
        try:
            await perform_full_sync()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Admin panel sync failed: {exc}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    try:
        data = await cache.dump(
            company_id=company_id,
            company_code=company_code,
            module_code=module_code,
            branch_code=branch_code,
            domain=domain,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    return {"count": len(data), "items": data}
