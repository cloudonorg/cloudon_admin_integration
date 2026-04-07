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


class WebhookSyncPayload(BaseModel):
    operation: Literal["upsert", "delete"] = "upsert"
    company_id: str | None = None
    company_code: int | str | None = None
    company_name: str | None = None
    infrastructure_id: str | None = None
    infrastructure_serial_num: str | None = None
    infrastructure_domain: str | None = None
    module_code: str | None = None
    module_name: str | None = None
    params: dict[str, Any] | None = None
    to_date: str | None = None
    state: str | None = None
    revoked_at: str | None = None


def _has_license_fields(payload: WebhookSyncPayload) -> bool:
    return any((payload.to_date, payload.state, payload.revoked_at))


def _has_company_fields(payload: WebhookSyncPayload) -> bool:
    return any(
        (
            payload.company_id,
            payload.company_code is not None,
            payload.company_name,
            payload.infrastructure_id,
            payload.infrastructure_serial_num,
            payload.infrastructure_domain,
        )
    )


def _has_module_metadata_fields(payload: WebhookSyncPayload) -> bool:
    return bool(payload.module_code and payload.module_name and not payload.params and not _has_license_fields(payload))


async def _apply_webhook_payload(
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
                        source="webhook",
                    ),
                }
            )

    if payload.module_code and _has_license_fields(payload):
        if operation == "delete":
            company_code = payload.company_code or 0
            result["applied"].append(
                {
                    "type": "license",
                    "result": await cache.delete_entitlement(
                        payload.infrastructure_domain,
                        company_code,
                        payload.module_code,
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
                        is_running=(payload.state or "").strip().lower() == "active",
                        license_to_date=payload.to_date,
                        license={
                            "expiration_date": payload.to_date,
                            "state": payload.state,
                            "revoked_at": payload.revoked_at,
                        },
                        state=payload.state,
                        revoked_at=payload.revoked_at,
                        source="webhook",
                    ),
                }
            )

    if payload.module_code and _has_module_metadata_fields(payload):
        if operation == "delete":
            result["applied"].append(
                {
                    "type": "module",
                    "result": await cache.delete_module_records(payload.module_code),
                }
            )
        else:
            result["applied"].append(
                {
                    "type": "module",
                    "result": await cache.upsert_module_metadata(
                        payload.module_code,
                        module_name=payload.module_name,
                        source="webhook",
                    ),
                }
            )

    if _has_company_fields(payload) and not payload.module_code and not _has_license_fields(payload) and payload.params is None:
        if operation == "delete":
            result["applied"].append(
                {
                    "type": "company",
                    "result": await cache.delete_company_records(
                        company_id=payload.company_id,
                        company_code=payload.company_code,
                        infrastructure_domain=payload.infrastructure_domain,
                    ),
                }
            )
        else:
            result["applied"].append(
                {
                    "type": "company",
                    "result": await cache.update_company_metadata(
                        company_id=payload.company_id,
                        company_code=payload.company_code,
                        company_name=payload.company_name,
                        infrastructure_id=payload.infrastructure_id,
                        infrastructure_serial_num=payload.infrastructure_serial_num,
                        infrastructure_domain=payload.infrastructure_domain,
                        source="webhook",
                    ),
                }
            )

    result["applied_count"] = len(result["applied"])
    return result


async def _refresh_all(cache: IntegrationCache) -> dict[str, Any]:
    return await perform_full_sync(cache=cache)


@sync_router.post("/sync-single-license", dependencies=[Depends(require_sync_key)])
async def sync_single_license(
    payload: SingleLicenseSyncPayload,
    cache: IntegrationCache = Depends(get_cache),
):
    try:
        result = await _apply_webhook_payload(
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
        result = await _apply_webhook_payload(
            WebhookSyncPayload(
                operation=payload.operation,
                company_id=payload.company_id,
                company_code=payload.company_code,
                company_name=payload.company_name,
                infrastructure_id=payload.infrastructure_id,
                infrastructure_serial_num=payload.infrastructure_serial_num,
                infrastructure_domain=payload.infrastructure_domain,
                module_code=payload.module_code,
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


@sync_router.post("/sync-company-change", dependencies=[Depends(require_sync_key)])
async def sync_company_change(payload: CompanySyncPayload, cache: IntegrationCache = Depends(get_cache)):
    try:
        result = await _apply_webhook_payload(
            WebhookSyncPayload(
                operation=payload.operation,
                company_id=payload.company_id,
                company_code=payload.company_code,
                company_name=payload.company_name,
                infrastructure_id=payload.infrastructure_id,
                infrastructure_serial_num=payload.infrastructure_serial_num,
                infrastructure_domain=payload.infrastructure_domain,
            ),
            cache=cache,
        )
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
        result = await _apply_webhook_payload(payload, cache=cache)
        return {"status": "ok", "operation": payload.operation, "result": result}
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
