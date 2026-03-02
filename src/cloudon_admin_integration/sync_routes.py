from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import logging

from cloudon_admin_integration.cache import IntegrationCache
from cloudon_admin_integration.config import IntegrationSettings
from cloudon_admin_integration.dependencies import (
    get_admin_client,
    get_cache,
    get_settings,
    perform_full_sync,
    require_sync_key,
)

sync_router = APIRouter(tags=["Integration Sync"])
auth_proxy_router = APIRouter(tags=["Integration Auth"])
logger = logging.getLogger(__name__)


class SingleLicenseSyncPayload(BaseModel):
    operation: Literal["upsert", "delete"]
    module_name: str | None = None
    module_code: str
    serial_num: str | None = None
    company_id: str | None = None
    company_code: int
    branch_code: int | None = None
    to_date: str | None = None
    state: str | None = None
    revoked_at: str | None = None


class SingleParamSyncPayload(BaseModel):
    operation: Literal["upsert", "delete"] = "upsert"
    module_code: str
    company_id: str | None = None
    company_code: int
    branch_code: int | None = None
    params: dict[str, Any] | None = None
    param_key: str | None = None
    param_value: Any = None


@sync_router.post("/sync-single-license", dependencies=[Depends(require_sync_key)])
async def sync_single_license(
    payload: SingleLicenseSyncPayload,
    cfg: IntegrationSettings = Depends(get_settings),
    cache: IntegrationCache = Depends(get_cache),
):
    if payload.module_code != cfg.app_module_code:
        logger.info(
            "Skipping single license sync due to module mismatch: payload=%s expected=%s",
            payload.module_code,
            cfg.app_module_code,
        )
        return {"status": "skipped", "reason": "module_mismatch"}

    if payload.operation == "delete":
        company_ids = await cache.resolve_company_ids(
            company_id=payload.company_id,
            company_code=payload.company_code,
        )
        if not company_ids:
            return {"status": "ok", "operation": "delete", "deleted": 0}
        if len(company_ids) > 1 and not payload.company_id:
            raise HTTPException(
                status_code=409,
                detail="Ambiguous company_code. Provide company_id in webhook payload.",
            )
        try:
            deleted = 0
            for company_id in company_ids:
                deleted += await cache.delete_entitlement(
                    company_id=company_id,
                    module_code=payload.module_code,
                    branch_code=payload.branch_code,
                )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
        return {"status": "ok", "operation": "delete", "deleted": deleted}

    state = (payload.state or "").lower()
    is_running = state in {"running", "active", "enabled", "true", "1"} and not payload.revoked_at
    company_ids = await cache.resolve_company_ids(
        company_id=payload.company_id,
        company_code=payload.company_code,
    )
    if not company_ids:
        raise HTTPException(status_code=404, detail="Company not found in cache mapping. Run full sync first.")
    if len(company_ids) > 1 and not payload.company_id:
        raise HTTPException(
            status_code=409,
            detail="Ambiguous company_code. Provide company_id in webhook payload.",
        )
    try:
        records = []
        for company_id in company_ids:
            record = await cache.upsert_license(
                company_id=company_id,
                company_code=payload.company_code,
                module_code=payload.module_code,
                branch_code=payload.branch_code,
                is_running=is_running,
                license_to_date=payload.to_date,
                state=payload.state,
                revoked_at=payload.revoked_at,
                source="webhook_single_license",
                metadata={"serial_num": payload.serial_num, "module_name": payload.module_name},
            )
            records.append(record)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    return {"status": "ok", "operation": "upsert", "records": records}


@sync_router.post("/sync-single-param", dependencies=[Depends(require_sync_key)])
async def sync_single_param(
    payload: SingleParamSyncPayload,
    cfg: IntegrationSettings = Depends(get_settings),
    cache: IntegrationCache = Depends(get_cache),
):
    if payload.module_code != cfg.app_module_code:
        logger.info(
            "Skipping single param sync due to module mismatch: payload=%s expected=%s",
            payload.module_code,
            cfg.app_module_code,
        )
        return {"status": "skipped", "reason": "module_mismatch"}

    if payload.operation == "delete":
        company_ids = await cache.resolve_company_ids(
            company_id=payload.company_id,
            company_code=payload.company_code,
        )
        if not company_ids:
            return {"status": "ok", "operation": "delete", "deleted": 0}
        if len(company_ids) > 1 and not payload.company_id:
            raise HTTPException(
                status_code=409,
                detail="Ambiguous company_code. Provide company_id in webhook payload.",
            )
        try:
            existing = await cache.get_entitlement(company_ids[0], payload.module_code, payload.branch_code)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
        if not existing:
            return {"status": "ok", "operation": "delete", "deleted": 0}
        if payload.param_key:
            params = existing.get("params") or {}
            params.pop(payload.param_key, None)
            try:
                record = await cache.upsert_params(
                    company_id=company_ids[0],
                    company_code=payload.company_code,
                    module_code=payload.module_code,
                    branch_code=payload.branch_code,
                    params=params,
                    source="webhook_single_param_delete",
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
            return {"status": "ok", "operation": "delete", "record": record}

        try:
            record = await cache.upsert_params(
                company_id=company_ids[0],
                company_code=payload.company_code,
                module_code=payload.module_code,
                branch_code=payload.branch_code,
                params={},
                source="webhook_single_param_clear",
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
        return {"status": "ok", "operation": "delete", "record": record}

    params = payload.params or {}
    if payload.param_key:
        params[payload.param_key] = payload.param_value
    company_ids = await cache.resolve_company_ids(
        company_id=payload.company_id,
        company_code=payload.company_code,
    )
    if not company_ids:
        raise HTTPException(status_code=404, detail="Company not found in cache mapping. Run full sync first.")
    if len(company_ids) > 1 and not payload.company_id:
        raise HTTPException(
            status_code=409,
            detail="Ambiguous company_code. Provide company_id in webhook payload.",
        )
    try:
        record = await cache.upsert_params(
            company_id=company_ids[0],
            company_code=payload.company_code,
            module_code=payload.module_code,
            branch_code=payload.branch_code,
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


class TokenProxyRequest(BaseModel):
    client_id: str
    client_secret: str
    branch_code: str | None = None
    company_code: str | None = None
    module_code: str | None = None


@auth_proxy_router.post("/auth/token")
async def token_proxy(
    payload: TokenProxyRequest,
    cfg: IntegrationSettings = Depends(get_settings),
    admin_client=Depends(get_admin_client),
):
    if not cfg.allow_token_proxy:
        raise HTTPException(status_code=404, detail="Token proxy is disabled")

    try:
        result = await admin_client.proxy_client_token(payload.model_dump(exclude_none=True))
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=f"Token proxy failed: {body}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Token proxy failed: {exc}") from exc

    return result
