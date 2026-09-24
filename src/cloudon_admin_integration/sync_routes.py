from typing import Any, Literal

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from cloudon_admin_integration.admin_client import AdminPanelClient
from cloudon_admin_integration.cache import IntegrationCache
from cloudon_admin_integration.dependencies import (
    get_admin_client,
    get_cache,
    perform_full_sync,
    reconcile_effective_configs,
    refresh_effective_config,
    require_sync_key,
)

sync_router = APIRouter(tags=["Integration Sync"])


class SingleLicenseSyncPayload(BaseModel):
    operation: Literal["upsert", "delete"]
    application_id: str | None = None
    application_status: str | None = None
    application_expires_at: str | None = None
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
    application_id: str | None = None
    application_status: str | None = None
    application_expires_at: str | None = None
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


def _normalize_operation(value: Any) -> str:
    raw = str(value or "upsert").strip().lower()
    if raw in {"delete", "deleted", "remove", "removed", "destroy", "destroyed"}:
        return "delete"
    return "upsert"


async def _apply_direct_effective_payload(
    item: dict[str, Any],
    *,
    cache: IntegrationCache,
    admin_client: AdminPanelClient,
) -> dict[str, Any] | None:
    record = admin_client._normalize_effective_config(item)
    if not record:
        return None
    operation = _normalize_operation(item.get("operation"))
    if operation == "delete" or record.get("deleted"):
        deleted = await cache.delete_effective_config(
            record.get("domain"),
            record.get("company_code"),
            record.get("module_code"),
            record.get("branch_code"),
        )
        return {
            "applied": [{"type": "effective_config", "operation": "delete", "deleted": deleted}],
            "applied_count": 1,
        }
    applied = await cache.upsert_effective_config(record)
    return {
        "applied": [
            {
                "type": "effective_config",
                "operation": "upsert",
                "module_code": applied.get("module_code"),
                "company_code": applied.get("company_code"),
                "domain": applied.get("domain"),
                "version": applied.get("version"),
            }
        ],
        "applied_count": 1,
    }


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
                        application_id=payload.application_id,
                        application_status=payload.application_status,
                        application_expires_at=payload.application_expires_at,
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
                        application_id=payload.application_id,
                        application_status=payload.application_status,
                        application_expires_at=payload.application_expires_at,
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


async def _apply_api_client_payload(item: dict[str, Any], *, cache: IntegrationCache) -> dict[str, Any] | None:
    """Store one client credential, so this service can recognise its caller.

    The panel sends the hash it holds, never a secret. Without this a service
    could read a company's licences but had no way to tell who was asking.
    """
    if str(item.get("event_type") or item.get("type") or "").split(".")[0] != "api_client":
        return None
    client_id = str(item.get("client_id") or "").strip()
    if not client_id:
        return None
    if _normalize_operation(item.get("operation")) == "delete" or item.get("deleted"):
        removed = await cache.delete_api_client(client_id)
        return {
            "applied": [{"type": "api_client", "operation": "delete", "client_id": client_id, "deleted": removed}],
            "applied_count": 1,
        }
    stored = await cache.upsert_api_client(
        {
            "client_id": client_id,
            "client_secret_hash": item.get("client_secret_hash"),
            "is_active": bool(item.get("is_active", True)),
            "token_ttl_seconds": item.get("token_ttl_seconds"),
            "company_id": item.get("company_id"),
            "company_code": item.get("company_code"),
            "company_name": item.get("company_name"),
            "infrastructure_id": item.get("infrastructure_id"),
            "infrastructure_domain": item.get("infrastructure_domain") or item.get("domain"),
            "infrastructure_serial_num": item.get("infrastructure_serial_num"),
            "updated_at": item.get("updated_at"),
        }
    )
    return {
        "applied": [
            {
                "type": "api_client",
                "operation": "upsert",
                "client_id": stored.get("client_id"),
                "company_code": stored.get("company_code"),
            }
        ],
        "applied_count": 1,
    }


async def _apply_sync_item(
    item: Any,
    *,
    cache: IntegrationCache,
    admin_client: AdminPanelClient,
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise HTTPException(
            status_code=400,
            detail={"reason": "sync_payload_invalid", "message": "Sync payload must be a JSON object"},
        )
    credential = await _apply_api_client_payload(item, cache=cache)
    if credential is not None:
        return credential
    direct = await _apply_direct_effective_payload(item, cache=cache, admin_client=admin_client)
    if direct is not None:
        return direct
    return await _apply_legacy_payload(WebhookSyncPayload(**item), cache=cache)


@sync_router.post("/sync-single-license", dependencies=[Depends(require_sync_key)])
async def sync_single_license(
    payload: SingleLicenseSyncPayload,
    cache: IntegrationCache = Depends(get_cache),
):
    try:
        result = await _apply_legacy_payload(
            WebhookSyncPayload(
                operation=payload.operation,
                application_id=payload.application_id,
                application_status=payload.application_status,
                application_expires_at=payload.application_expires_at,
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
    payload: Any = Body(default=None),
    cache: IntegrationCache = Depends(get_cache),
    admin_client: AdminPanelClient = Depends(get_admin_client),
):
    try:
        payload = payload or {}
        items = payload if isinstance(payload, list) else [payload]
        results = [await _apply_sync_item(item, cache=cache, admin_client=admin_client) for item in items]
        if len(results) == 1:
            return {"status": "ok", "operation": "sync", "result": results[0]}
        return {
            "status": "ok",
            "operation": "batch",
            "result": {
                "applied": results,
                "applied_count": sum(item.get("applied_count", 0) for item in results),
            },
        }
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
    all_companies: bool = Query(default=False),
    cache: IntegrationCache = Depends(get_cache),
):
    if refresh:
        try:
            await perform_full_sync(cache=cache)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Admin panel sync failed: {exc}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=f"Cache unavailable: {exc}") from exc
    # Unfiltered, this returns every tenant this middleware has ever cached. Make
    # a fleet-wide dump an explicit request rather than the default.
    scoped = any(value is not None for value in (company_id, company_code, module_code, domain))
    if not scoped and not all_companies:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "scope_required",
                "message": (
                    "Filter by company_id, company_code, module_code or domain, "
                    "or pass all_companies=true to dump every cached tenant."
                ),
            },
        )
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
