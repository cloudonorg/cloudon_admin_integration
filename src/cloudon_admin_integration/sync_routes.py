from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from cloudon_admin_integration.cache import IntegrationCache
from cloudon_admin_integration.dependencies import (
    get_cache,
    perform_full_sync,
    reconcile_effective_configs,
    refresh_effective_config,
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
    branch_code: int | None = None
    to_date: str | None = None
    state: str | None = None
    revoked_at: str | None = None
    version: int | None = None


class SingleParamSyncPayload(BaseModel):
    operation: Literal["upsert", "delete"] = "upsert"
    module_code: str
    company_id: str | None = None
    company_code: int
    company_name: str | None = None
    infrastructure_id: str | None = None
    infrastructure_serial_num: str | None = None
    infrastructure_domain: str
    branch_code: int | None = None
    params: dict[str, Any] | None = None
    version: int | None = None


class WebhookSyncPayload(BaseModel):
    event_id: int | None = None
    event_type: str | None = None
    scope: str | None = None
    operation: Literal["upsert", "delete"] = "upsert"
    company_id: str | None = None
    company_code: int | str | None = None
    company_name: str | None = None
    infrastructure_id: str | None = None
    infrastructure_serial_num: str | None = None
    infrastructure_domain: str | None = None
    module_code: str | None = None
    module_name: str | None = None
    branch_code: int | None = None
    version: int | None = None
    params: dict[str, Any] | None = None
    to_date: str | None = None
    state: str | None = None
    revoked_at: str | None = None


async def _apply_legacy_payload(
    payload: WebhookSyncPayload,
    *,
    cache: IntegrationCache,
) -> dict[str, Any]:
    result: dict[str, Any] = {"applied": []}
    operation = (payload.operation or "upsert").lower()

    if payload.module_code and payload.params is not None:
        if operation == "delete":
            result["applied"].append(
                {
                    "type": "params",
                    "result": await cache.clear_params(
                        payload.infrastructure_domain,
                        payload.company_code or 0,
                        payload.module_code,
                        payload.branch_code,
                    ),
                }
            )
        else:
            result["applied"].append(
                {
                    "type": "params",
                    "result": await cache.upsert_params(
                        payload.infrastructure_domain,
                        payload.company_code or 0,
                        payload.module_code,
                        payload.params or {},
                        company_id=payload.company_id,
                        infrastructure_id=payload.infrastructure_id,
                        infrastructure_serial_num=payload.infrastructure_serial_num,
                        company_name=payload.company_name,
                        infrastructure_domain=payload.infrastructure_domain,
                        module_name=payload.module_name,
                        branch_code=payload.branch_code,
                        version=payload.version,
                        source="legacy_webhook",
                    ),
                }
            )

    if payload.module_code and any((payload.to_date, payload.state, payload.revoked_at)):
        if operation == "delete":
            result["applied"].append(
                {
                    "type": "license",
                    "result": await cache.delete_effective_config(
                        payload.infrastructure_domain,
                        payload.company_code or 0,
                        payload.module_code,
                        payload.branch_code,
                    ),
                }
            )
        else:
            result["applied"].append(
                {
                    "type": "license",
                    "result": await cache.upsert_license(
                        payload.infrastructure_domain,
                        payload.company_code or 0,
                        payload.module_code,
                        company_id=payload.company_id,
                        infrastructure_id=payload.infrastructure_id,
                        infrastructure_serial_num=payload.infrastructure_serial_num,
                        company_name=payload.company_name,
                        infrastructure_domain=payload.infrastructure_domain,
                        module_name=payload.module_name,
                        branch_code=payload.branch_code,
                        version=payload.version,
                        is_running=(payload.state or "").strip().lower() == "active",
                        license_to_date=payload.to_date,
                        license={
                            "expiration_date": payload.to_date,
                            "status": payload.state,
                            "state": payload.state,
                            "revoked_at": payload.revoked_at,
                        },
                        state=payload.state,
                        revoked_at=payload.revoked_at,
                        source="legacy_webhook",
                    ),
                }
            )

    result["applied_count"] = len(result["applied"])
    return result


async def _apply_notification_payload(
    payload: WebhookSyncPayload,
    *,
    cache: IntegrationCache,
) -> dict[str, Any]:
    if payload.module_code:
        if payload.branch_code is not None:
            record = await refresh_effective_config(payload.module_code, branch_code=payload.branch_code, cache=cache)
            return {"applied": [{"type": "effective_config", "module_code": payload.module_code, "branch_code": payload.branch_code, "version": record.get("version")}], "applied_count": 1}
        record = await refresh_effective_config(payload.module_code, branch_code=None, cache=cache)
        return {"applied": [{"type": "effective_config", "module_code": payload.module_code, "version": record.get("version")}], "applied_count": 1}
    result = await reconcile_effective_configs(since_version=payload.version or None, cache=cache)
    return {"applied": [{"type": "reconcile", **result}], "applied_count": 1}


@sync_router.post("/sync-single-license", dependencies=[Depends(require_sync_key)])
async def sync_single_license(
    payload: SingleLicenseSyncPayload,
    cache: IntegrationCache = Depends(get_cache),
):
    try:
        result = await _apply_legacy_payload(
            WebhookSyncPayload(
                operation=payload.operation,
                company_id=payload.company_id,
                company_code=payload.company_code,
                company_name=payload.company_name,
                infrastructure_id=payload.infrastructure_id,
                infrastructure_serial_num=payload.infrastructure_serial_num,
                infrastructure_domain=payload.infrastructure_domain,
                module_code=payload.module_code,
                module_name=payload.module_name,
                branch_code=payload.branch_code,
                version=payload.version,
                to_date=payload.to_date,
                state=payload.state,
                revoked_at=payload.revoked_at,
            ),
            cache=cache,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    return {"status": "ok", "operation": payload.operation, "result": result}


@sync_router.post("/sync-single-param", dependencies=[Depends(require_sync_key)])
async def sync_single_param(
    payload: SingleParamSyncPayload,
    cache: IntegrationCache = Depends(get_cache),
):
    try:
        result = await _apply_legacy_payload(
            WebhookSyncPayload(
                operation=payload.operation,
                company_id=payload.company_id,
                company_code=payload.company_code,
                company_name=payload.company_name,
                infrastructure_id=payload.infrastructure_id,
                infrastructure_serial_num=payload.infrastructure_serial_num,
                infrastructure_domain=payload.infrastructure_domain,
                module_code=payload.module_code,
                branch_code=payload.branch_code,
                version=payload.version,
                params=payload.params,
            ),
            cache=cache,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    return {"status": "ok", "operation": payload.operation, "result": result}


class CompanySyncPayload(BaseModel):
    operation: Literal["upsert", "delete"] = "upsert"
    company_id: str | None = None
    company_code: int | str | None = None
    company_name: str | None = None
    infrastructure_id: str | None = None
    infrastructure_serial_num: str | None = None
    infrastructure_domain: str | None = None
    version: int | None = None


@sync_router.post("/sync-company-change", dependencies=[Depends(require_sync_key)])
async def sync_company_change(payload: CompanySyncPayload, cache: IntegrationCache = Depends(get_cache)):
    try:
        result = await reconcile_effective_configs(since_version=payload.version, cache=cache)
        return {
            "status": "ok",
            "operation": payload.operation,
            "requested_company_code": payload.company_code,
            "result": result,
        }
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Admin panel sync failed: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc


@sync_router.post("/sync-redis-data", dependencies=[Depends(require_sync_key)])
async def sync_redis_data(
    payload: WebhookSyncPayload,
    cache: IntegrationCache = Depends(get_cache),
):
    try:
        if payload.event_id or payload.version or payload.event_type:
            result = await _apply_notification_payload(payload, cache=cache)
        else:
            result = await _apply_legacy_payload(payload, cache=cache)
        return {"status": "ok", "operation": payload.operation, "result": result}
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
            await perform_full_sync(cache=cache)
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
