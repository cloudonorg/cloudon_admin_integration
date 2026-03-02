import logging
from datetime import datetime
from typing import Any

import httpx
from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from cloudon_admin_integration.admin_client import AdminPanelClient
from cloudon_admin_integration.cache import IntegrationCache, is_license_current
from cloudon_admin_integration.config import IntegrationSettings, settings
from cloudon_admin_integration.security import ApiClientClaims, require_valid_api_client_token

logger = logging.getLogger(__name__)

_cache = IntegrationCache(settings)
_admin_client = AdminPanelClient(settings)


def get_settings() -> IntegrationSettings:
    return settings


def get_cache() -> IntegrationCache:
    return _cache


def get_admin_client() -> AdminPanelClient:
    return _admin_client


class EntitlementContext(BaseModel):
    company_id: str
    company_code: int | None = None
    module_code: str
    branch_code: int | None = None
    matched_branch: int | None = None
    is_running: bool = False
    license_to_date: str | None = None
    state: str | None = None
    revoked_at: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    claims: dict[str, Any] = Field(default_factory=dict)


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


async def require_sync_key(
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    cfg: IntegrationSettings = Depends(get_settings),
) -> None:
    if not cfg.sync_key:
        raise HTTPException(status_code=500, detail="SYNC_KEY is not configured")
    if x_sync_key != cfg.sync_key:
        raise HTTPException(status_code=401, detail="Invalid X-Sync-Key")


async def _require_module_entitlement(
    request: Request,
    *,
    module_code: str,
    claims: ApiClientClaims,
    cache: IntegrationCache,
    cfg: IntegrationSettings,
) -> EntitlementContext:
    header_company_code = _to_int_or_none(request.headers.get("X-Company-Code"))
    header_company_id = (request.headers.get("X-Company-Id") or "").strip() or None
    header_branch_code = _to_int_or_none(request.headers.get("X-Branch-Code"))

    token_company_id = str(claims.company_id).strip() if claims.company_id is not None else None
    company_id = header_company_id or token_company_id
    company_code = header_company_code or claims.company_code

    if not company_id and claims.client_id:
        mapping = await cache.get_company_by_client_id(claims.client_id)
        if mapping:
            company_id = str(mapping.get("company_id") or "").strip() or None
            if company_code is None:
                company_code = _to_int_or_none(mapping.get("company_code"))

    if not company_id and company_code:
        company_ids = await cache.resolve_company_ids(company_code=company_code)
        if len(company_ids) == 1:
            company_id = company_ids[0]
        elif len(company_ids) > 1:
            raise HTTPException(
                status_code=403,
                detail={
                    "reason": "company_ambiguous",
                    "message": "Multiple companies match company_code. Provide X-Company-Id or token company_id.",
                    "company_code": company_code,
                    "company_ids": company_ids,
                },
            )

    if not company_id:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "company_missing",
                "message": "Could not resolve company_id from token/client mapping/header",
            },
        )

    token_module = claims.module_code
    if token_module and token_module not in {module_code, "*"}:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "token_module_mismatch",
                "message": "Token is not valid for this module",
                "token_module_code": token_module,
                "expected_module_code": module_code,
            },
        )

    branch_code = header_branch_code or claims.branch_code
    try:
        record = await cache.get_entitlement(
            company_id=company_id,
            module_code=module_code,
            branch_code=branch_code,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason": "cache_unavailable", "message": str(exc)},
        ) from exc
    if not record:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "license_not_found",
                "message": "No cached license found for company/module/branch",
                "company_id": company_id,
                "company_code": company_code,
                "module_code": module_code,
                "branch_code": branch_code,
            },
        )

    if not record.get("is_running"):
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "license_not_running",
                "message": "License is not running",
                "company_id": company_id,
                "company_code": record.get("company_code") or company_code,
                "module_code": module_code,
                "branch_code": record.get("branch_code"),
            },
        )

    if not is_license_current(
        record.get("license_to_date"),
        bool(record.get("is_running")),
        cfg.license_extension_days,
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "license_expired",
                "message": "License has expired",
                "company_id": company_id,
                "company_code": record.get("company_code") or company_code,
                "module_code": module_code,
                "branch_code": record.get("branch_code"),
                "license_to_date": record.get("license_to_date"),
                "license_extension_days": cfg.license_extension_days,
            },
        )

    return EntitlementContext(
        company_id=company_id,
        company_code=_to_int_or_none(record.get("company_code")) or company_code,
        module_code=module_code,
        branch_code=branch_code,
        matched_branch=_to_int_or_none(record.get("_matched_branch")),
        is_running=bool(record.get("is_running")),
        license_to_date=record.get("license_to_date"),
        state=record.get("state"),
        revoked_at=record.get("revoked_at"),
        params=record.get("params") or {},
        claims=claims.raw,
    )


async def require_module_entitlement(
    request: Request,
    claims: ApiClientClaims = Depends(require_valid_api_client_token),
    cache: IntegrationCache = Depends(get_cache),
    cfg: IntegrationSettings = Depends(get_settings),
) -> EntitlementContext:
    return await _require_module_entitlement(
        request,
        module_code=cfg.app_module_code,
        claims=claims,
        cache=cache,
        cfg=cfg,
    )


def require_module_entitlement_for(module_code: str):
    async def _dep(
        request: Request,
        claims: ApiClientClaims = Depends(require_valid_api_client_token),
        cache: IntegrationCache = Depends(get_cache),
        cfg: IntegrationSettings = Depends(get_settings),
    ) -> EntitlementContext:
        return await _require_module_entitlement(
            request,
            module_code=module_code,
            claims=claims,
            cache=cache,
            cfg=cfg,
        )

    return _dep


async def perform_full_sync(
    cache: IntegrationCache | None = None,
    admin_client: AdminPanelClient | None = None,
) -> dict[str, Any]:
    cache_client = cache or _cache
    api_client = admin_client or _admin_client

    payload = await api_client.fetch_full_sync_payload()
    records = api_client.normalize_full_sync_records(payload)
    client_mappings = api_client.normalize_client_mappings(payload)

    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    for record in records:
        record["updated_at"] = now
        record["source"] = "full_sync"

    rebuild_info = await cache_client.rebuild(records, client_mappings=client_mappings)
    return {
        "records": len(records),
        "rebuild": rebuild_info,
        "module_code": settings.app_module_code,
    }


async def startup_integration() -> None:
    try:
        await _cache.connect()
        logger.info("Integration Redis connected")
    except Exception as exc:  # pragma: no cover
        logger.warning("Integration Redis unavailable during startup: %s", exc)
        return

    if settings.sync_on_startup:
        try:
            result = await perform_full_sync()
            logger.info("Initial full sync complete: %s", result)
        except httpx.HTTPError as exc:
            logger.warning("Initial full sync skipped (admin panel unavailable): %s", exc)
        except Exception as exc:  # pragma: no cover
            logger.warning("Initial full sync failed: %s", exc)


async def shutdown_integration() -> None:
    try:
        await _cache.disconnect()
    except Exception as exc:  # pragma: no cover
        logger.warning("Error while closing integration Redis client: %s", exc)
