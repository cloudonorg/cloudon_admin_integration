import logging
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Sequence
from typing import Any

import httpx
from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from cloudon_admin_integration.admin_client import AdminPanelClient
from cloudon_admin_integration.cache import IntegrationCache, is_license_current, parse_date_or_none
from cloudon_admin_integration.config import IntegrationSettings, settings
from cloudon_admin_integration.security import ApiClientClaims, require_valid_api_client_token

logger = logging.getLogger(__name__)

_cache = IntegrationCache(settings)
_admin_client = AdminPanelClient(settings)


@dataclass(frozen=True)
class _ResolvedEntitlementScope:
    client_id: str | None
    company_id: str
    company_code: int
    domain: str
    branch_code: int | None
    session: dict[str, Any] | None


def get_settings() -> IntegrationSettings:
    return settings


def get_cache() -> IntegrationCache:
    return _cache


def get_admin_client() -> AdminPanelClient:
    return _admin_client


class EntitlementContext(BaseModel):
    company_id: str
    company_code: int | None = None
    company_name: str | None = None
    module_code: str
    infrastructure_domain: str | None = None
    infrastructure_serial_num: str | None = None
    branch_code: int | None = None
    matched_branch: int | None = None
    selected_branch: dict[str, Any] | None = None
    is_running: bool = False
    license_to_date: str | None = None
    state: str | None = None
    revoked_at: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    claims: dict[str, Any] = Field(default_factory=dict)


class EntitlementsContext(BaseModel):
    client_id: str | None = None
    company_id: str
    company_code: int | None = None
    company_name: str | None = None
    infrastructure_domain: str | None = None
    infrastructure_serial_num: str | None = None
    branch_code: int | None = None
    entitlements: list[EntitlementContext] = Field(default_factory=list)
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


def _fail(status_code: int, reason: str, message: str) -> None:
    raise HTTPException(status_code=status_code, detail={"reason": reason, "message": message})


def _normalize_module_codes(value: str | Sequence[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = tuple(part.strip() for part in value.split(","))
    else:
        items = tuple(value)
    out: list[str] = []
    for item in items:
        for chunk in str(item).split(","):
            code = chunk.strip()
            if code and code not in out:
                out.append(code)
    return tuple(out)


async def _resolve_entitlement_scope(
    request: Request,
    claims: ApiClientClaims,
    cache: IntegrationCache,
) -> _ResolvedEntitlementScope:
    header_domain = (request.headers.get("X-Infrastructure-Domain") or request.headers.get("X-Domain") or "").strip() or None
    header_company_code = _to_int_or_none(request.headers.get("X-Company-Code"))
    header_company_id = (request.headers.get("X-Company-Id") or "").strip() or None
    header_branch_code = _to_int_or_none(request.headers.get("X-Branch-Code"))
    session = await cache.get_client_session(claims.client_id) if claims.client_id else None

    token_company_id = str(claims.company_id).strip() if claims.company_id is not None else None
    company_id = header_company_id or token_company_id
    company_code = header_company_code if header_company_code is not None else claims.company_code
    domain = header_domain or claims.infrastructure_domain

    if session:
        company_id = company_id or str(session.get("company_id") or "").strip() or None
        if company_code is None:
            company_code = _to_int_or_none(session.get("company_code"))
        domain = domain or (session.get("infrastructure_domain") or None)

    if not company_id:
        _fail(403, "company_missing", "Could not resolve company_id from token/client session/header")
    if company_code is None:
        _fail(403, "company_code_missing", "Could not resolve company_code from token/session/header")
    if not domain:
        _fail(403, "domain_missing", "Could not resolve infrastructure domain from token/session/header")

    return _ResolvedEntitlementScope(
        client_id=claims.client_id,
        company_id=company_id,
        company_code=company_code,
        domain=domain,
        branch_code=header_branch_code if header_branch_code is not None else claims.branch_code,
        session=session,
    )


def _build_entitlement_context(
    record: dict[str, Any],
    *,
    scope: _ResolvedEntitlementScope,
    claims: ApiClientClaims,
    module_code: str,
) -> EntitlementContext:
    record_company_code = _to_int_or_none(record.get("company_code"))
    company = record.get("company") if isinstance(record.get("company"), dict) else {}

    return EntitlementContext(
        company_id=str(record.get("company_id") or scope.company_id),
        company_code=record_company_code if record_company_code is not None else scope.company_code,
        company_name=company.get("name") if isinstance(company, dict) else claims.company_name,
        module_code=module_code,
        infrastructure_domain=(record.get("domain") or scope.domain),
        infrastructure_serial_num=record.get("infrastructure_serial_num") or claims.infrastructure_serial_num,
        branch_code=scope.branch_code,
        matched_branch=_to_int_or_none(record.get("_matched_branch")),
        selected_branch=record.get("_selected_branch"),
        is_running=bool(record.get("is_running")),
        license_to_date=record.get("license_to_date"),
        state=record.get("state"),
        revoked_at=record.get("revoked_at"),
        params=record.get("params") or {},
        claims=claims.raw,
    )


def _set_expiry_warning(request: Request, records: list[dict[str, Any]], cfg: IntegrationSettings) -> None:
    warning_days_left: int | None = None
    for record in records:
        if not record.get("is_running"):
            continue
        license_date = parse_date_or_none(record.get("license_to_date"))
        if not license_date:
            continue
        days_left = (license_date - datetime.utcnow().date()).days
        if 0 <= days_left <= cfg.license_expiry_warning_days:
            warning_days_left = days_left if warning_days_left is None else min(warning_days_left, days_left)

    if warning_days_left is not None:
        request.state.integration_message = f"License will expire in {warning_days_left} day(s)"


def _build_entitlements_context(
    *,
    scope: _ResolvedEntitlementScope,
    claims: ApiClientClaims,
    records: list[dict[str, Any]],
    entitlements: list[EntitlementContext],
) -> EntitlementsContext:
    first_record = records[0] if records else {}
    company = first_record.get("company") if isinstance(first_record.get("company"), dict) else {}

    return EntitlementsContext(
        client_id=scope.client_id,
        company_id=scope.company_id,
        company_code=scope.company_code,
        company_name=company.get("name") or claims.company_name,
        infrastructure_domain=scope.domain,
        infrastructure_serial_num=first_record.get("infrastructure_serial_num") or claims.infrastructure_serial_num,
        branch_code=scope.branch_code,
        entitlements=entitlements,
        claims=claims.raw,
    )


async def _load_cached_entitlements(
    cache: IntegrationCache,
    scope: _ResolvedEntitlementScope,
    *,
    module_codes: str | Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        records = await cache.dump(
            company_id=scope.company_id,
            company_code=scope.company_code,
            module_code=module_codes,
            branch_code=scope.branch_code,
            domain=scope.domain,
        )
        if records or not scope.company_id:
            return records
        return await cache.dump(
            company_code=scope.company_code,
            module_code=module_codes,
            branch_code=scope.branch_code,
            domain=scope.domain,
        )
    except RuntimeError as exc:
        _fail(503, "cache_unavailable", str(exc))


async def require_sync_key(
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    cfg: IntegrationSettings = Depends(get_settings),
) -> None:
    if not cfg.sync_key:
        _fail(500, "sync_key_missing", "SYNC_KEY is not configured")
    if x_sync_key != cfg.sync_key:
        _fail(401, "sync_key_invalid", "Invalid X-Sync-Key")


async def _require_module_entitlement(
    request: Request,
    *,
    module_code: str,
    claims: ApiClientClaims,
    cache: IntegrationCache,
    cfg: IntegrationSettings,
) -> EntitlementContext:
    scope = await _resolve_entitlement_scope(request, claims, cache)

    token_module = claims.module_code
    if token_module and token_module not in {module_code, "*"}:
        _fail(403, "token_module_mismatch", "Token is not valid for this module")

    try:
        record = await cache.get_entitlement(
            domain=scope.domain,
            company_code=scope.company_code,
            module_code=module_code,
            branch_code=scope.branch_code,
        )
    except RuntimeError as exc:
        _fail(503, "cache_unavailable", str(exc))
    if not record:
        _fail(403, "license_not_found", "No cached license found for company/module/branch")

    if not record.get("is_running"):
        _fail(403, "license_not_running", "License is not running")

    if not is_license_current(
        record.get("license_to_date"),
        bool(record.get("is_running")),
        cfg.license_extension_days,
    ):
        _fail(403, "license_expired", "License has expired")

    if cfg.require_module_params and not (record.get("params") or {}):
        _fail(403, "params_not_found", "Params not found for this module")

    warning_message = None
    license_date = parse_date_or_none(record.get("license_to_date"))
    if license_date:
        days_left = (license_date - datetime.utcnow().date()).days
        if 0 <= days_left <= cfg.license_expiry_warning_days:
            warning_message = f"License will expire in {days_left} day(s)"
    if warning_message:
        request.state.integration_message = warning_message

    return _build_entitlement_context(record, scope=scope, claims=claims, module_code=module_code)


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


async def require_module_entitlements(
    request: Request,
    claims: ApiClientClaims = Depends(require_valid_api_client_token),
    cache: IntegrationCache = Depends(get_cache),
    cfg: IntegrationSettings = Depends(get_settings),
) -> EntitlementsContext:
    scope = await _resolve_entitlement_scope(request, claims, cache)
    records = await _load_cached_entitlements(cache, scope)
    entitlements: list[EntitlementContext] = []
    for record in records:
        record_module_code = record.get("module_code")
        if not record_module_code:
            continue
        entitlements.append(
            _build_entitlement_context(
                record,
                scope=scope,
                claims=claims,
                module_code=str(record_module_code),
            )
        )
    _set_expiry_warning(request, records, cfg)
    return _build_entitlements_context(scope=scope, claims=claims, records=records, entitlements=entitlements)


def require_module_entitlements_for(module_codes: str | Sequence[str]):
    async def _dep(
        request: Request,
        claims: ApiClientClaims = Depends(require_valid_api_client_token),
        cache: IntegrationCache = Depends(get_cache),
        cfg: IntegrationSettings = Depends(get_settings),
    ) -> EntitlementsContext:
        normalized_module_codes = _normalize_module_codes(module_codes)
        if not normalized_module_codes:
            _fail(500, "module_codes_missing", "At least one module code is required")
        scope = await _resolve_entitlement_scope(request, claims, cache)
        records = await _load_cached_entitlements(cache, scope, module_codes=normalized_module_codes)
        entitlements: list[EntitlementContext] = []
        for record in records:
            record_module_code = record.get("module_code") or normalized_module_codes[0]
            if not record_module_code:
                continue
            entitlements.append(
                _build_entitlement_context(
                    record,
                    scope=scope,
                    claims=claims,
                    module_code=str(record_module_code),
                )
            )
        _set_expiry_warning(request, records, cfg)
        return _build_entitlements_context(scope=scope, claims=claims, records=records, entitlements=entitlements)

    return _dep


async def perform_full_sync(
    cache: IntegrationCache | None = None,
    admin_client: AdminPanelClient | None = None,
) -> dict[str, Any]:
    cache_client = cache or _cache
    api_client = admin_client or _admin_client

    payload = await api_client.bootstrap_client_bundle()
    records, client_session = api_client.normalize_bootstrap_bundle(payload)

    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    for record in records:
        record["updated_at"] = now
        record["source"] = "bootstrap"

    rebuild_info = await cache_client.rebuild(records, client_session=client_session)
    return {
        "records": len(records),
        "rebuild": rebuild_info,
        "module_code": settings.app_module_code,
        "module_codes": settings.app_module_codes,
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
            logger.info("Initial bootstrap sync complete: %s", result)
        except httpx.HTTPError as exc:
            logger.warning("Initial bootstrap sync skipped (admin panel unavailable): %s", exc)
        except Exception as exc:  # pragma: no cover
            logger.warning("Initial bootstrap sync failed: %s", exc)


async def shutdown_integration() -> None:
    try:
        await _cache.disconnect()
    except Exception as exc:  # pragma: no cover
        logger.warning("Error while closing integration Redis client: %s", exc)
