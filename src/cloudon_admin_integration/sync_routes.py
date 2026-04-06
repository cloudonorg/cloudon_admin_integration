from typing import Any, Literal

import httpx
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from cloudon_admin_integration.cache import IntegrationCache
from cloudon_admin_integration.config import IntegrationSettings
from cloudon_admin_integration.dependencies import (
    get_cache,
    get_settings,
    perform_full_sync,
    require_sync_key,
)

sync_router = APIRouter(tags=["Integration Sync"])
logger = logging.getLogger(__name__)


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


@sync_router.post("/sync-single-license", dependencies=[Depends(require_sync_key)])
async def sync_single_license(
    payload: SingleLicenseSyncPayload,
    cfg: IntegrationSettings = Depends(get_settings),
    cache: IntegrationCache = Depends(get_cache),
):
    state = (payload.state or "").lower()
    is_running = state in {"running", "active", "enabled", "true", "1"} and not payload.revoked_at

    try:
        if payload.operation == "delete":
            deleted = await cache.delete_entitlement(
                payload.infrastructure_domain,
                payload.company_code,
                payload.module_code,
            )
            return {"status": "ok", "operation": "delete", "deleted": deleted}

        record = await cache.upsert_license(
            domain=payload.infrastructure_domain,
            company_code=payload.company_code,
            company_id=payload.company_id,
            module_code=payload.module_code,
            company_name=payload.company_name,
            infrastructure_id=payload.infrastructure_id,
            infrastructure_serial_num=payload.infrastructure_serial_num,
            is_running=is_running,
            license_to_date=payload.to_date,
            license={
                "expiration_date": payload.to_date,
                "state": payload.state,
                "status": "active" if is_running else (state or "inactive"),
                "revoked_at": payload.revoked_at,
            },
            state=payload.state,
            revoked_at=payload.revoked_at,
            source="webhook_single_license",
            metadata={"serial_num": payload.serial_num, "module_name": payload.module_name},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    return {"status": "ok", "operation": "upsert", "record": record}


@sync_router.post("/sync-single-param", dependencies=[Depends(require_sync_key)])
async def sync_single_param(
    payload: SingleParamSyncPayload,
    cfg: IntegrationSettings = Depends(get_settings),
    cache: IntegrationCache = Depends(get_cache),
):
    params = payload.params or {}
    if payload.param_key:
        params[payload.param_key] = payload.param_value

    try:
        if payload.operation == "delete":
            if payload.param_key:
                existing = await cache.get_entitlement(
                    payload.infrastructure_domain,
                    payload.company_code,
                    payload.module_code,
                )
                if not existing:
                    return {"status": "ok", "operation": "delete", "deleted": 0}
                current_params = dict(existing.get("params") or {})
                current_params.pop(payload.param_key, None)
                record = await cache.upsert_params(
                    domain=payload.infrastructure_domain,
                    company_code=payload.company_code,
                    company_id=payload.company_id,
                    module_code=payload.module_code,
                    company_name=payload.company_name,
                    infrastructure_id=payload.infrastructure_id,
                    infrastructure_serial_num=payload.infrastructure_serial_num,
                    params=current_params,
                    source="webhook_single_param_delete",
                )
                return {"status": "ok", "operation": "delete", "record": record}

            record = await cache.upsert_params(
                domain=payload.infrastructure_domain,
                company_code=payload.company_code,
                company_id=payload.company_id,
                module_code=payload.module_code,
                company_name=payload.company_name,
                infrastructure_id=payload.infrastructure_id,
                infrastructure_serial_num=payload.infrastructure_serial_num,
                params={},
                source="webhook_single_param_clear",
            )
            return {"status": "ok", "operation": "delete", "record": record}

        record = await cache.upsert_params(
            domain=payload.infrastructure_domain,
            company_code=payload.company_code,
            company_id=payload.company_id,
            module_code=payload.module_code,
            company_name=payload.company_name,
            infrastructure_id=payload.infrastructure_id,
            infrastructure_serial_num=payload.infrastructure_serial_num,
            params=params,
            source="webhook_single_param",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    return {"status": "ok", "operation": "upsert", "record": record}


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
        return {"status": "ok", "result": result}
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
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    return {"count": len(data), "items": data}
