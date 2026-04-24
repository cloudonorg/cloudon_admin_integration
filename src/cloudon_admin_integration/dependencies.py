import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Sequence
from typing import Any

import httpx
from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, RootModel

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


async def bootstrap_and_cache_client(
    client_id: str,
    client_secret: str,
    *,
    branch_code: str | None = None,
    module_code: str | None = None,
    cache: IntegrationCache | None = None,
    admin_client: AdminPanelClient | None = None,
) -> dict[str, Any]:
    cache_client = cache or _cache
    api_client = admin_client or _admin_client

    payload = await api_client.bootstrap_client_bundle(
        client_id=client_id,
        client_secret=client_secret,
        branch_code=branch_code,
        module_code=module_code,
    )
    records, client_session = api_client.normalize_bootstrap_bundle(payload)
    algorithm = settings.admin_panel_jwt_algorithm.upper()
    client_session["client_secret"] = client_secret
    if algorithm.startswith("HS"):
        client_session["verification_key"] = client_secret
    else:
        client_session.pop("verification_key", None)
    rebuild_info = await cache_client.rebuild(records, client_session=client_session)

    response = dict(payload)
    response["cache"] = rebuild_info
    response["records"] = len(records)
    return response


async def refresh_effective_config(
    module_code: str,
    *,
    branch_code: int | str | None = None,
    cache: IntegrationCache | None = None,
    admin_client: AdminPanelClient | None = None,
) -> dict[str, Any]:
    cache_client = cache or _cache
    api_client = admin_client or _admin_client
    payload = await api_client.resolve_effective_config(module_code=module_code, branch_code=branch_code)
    record = api_client._normalize_effective_config(payload.get("effective_config") or {})
    if not record:
        raise httpx.HTTPError("Effective config not found")
    await cache_client.upsert_effective_config(record)
    session = await cache_client.get_client_session(settings.admin_panel_client_id) if settings.admin_panel_client_id else None
    if session is not None and payload.get("sync_cursor") is not None:
        session["sync_cursor"] = int(payload.get("sync_cursor") or 0)
        await cache_client.store_client_session(session)
    return record


async def reconcile_effective_configs(
    *,
    since_version: int | None = None,
    cache: IntegrationCache | None = None,
    admin_client: AdminPanelClient | None = None,
) -> dict[str, Any]:
    cache_client = cache or _cache
    api_client = admin_client or _admin_client
    current_cursor = since_version if since_version is not None else await cache_client.get_sync_cursor()
    payload = await api_client.reconcile_effective_configs(since_version=current_cursor)
    records = [
        record
        for item in (payload.get("effective_configs") or [])
        if isinstance(item, dict)
        if (record := api_client._normalize_effective_config(item))
    ]
    replaced = 0
    deleted = 0
    for record in records:
        if record.get("deleted"):
            await cache_client.delete_effective_config(
                record.get("domain"),
                record.get("company_code"),
                record.get("module_code"),
                record.get("branch_code"),
            )
            deleted += 1
            continue
        await cache_client.upsert_effective_config(record)
        replaced += 1
    sync_cursor = int(payload.get("sync_cursor") or current_cursor or 0)
    await cache_client.set_sync_cursor(sync_cursor)
    return {"replaced": replaced, "deleted": deleted, "cursor": sync_cursor, "records": len(records)}


class EntitlementLicenseContext(BaseModel):
    expiration_date: str | None = None
    status: str | None = None


class EntitlementContext(BaseModel):
    module: str
    license: EntitlementLicenseContext
    parameters: dict[str, Any] = Field(default_factory=dict)
    effective_config: dict[str, Any] = Field(default_factory=dict)

    company_id: str | None = Field(default=None, exclude=True)
    company_code: int | None = Field(default=None, exclude=True)
    company_name: str | None = Field(default=None, exclude=True)
    infrastructure_domain: str | None = Field(default=None, exclude=True)
    infrastructure_serial_num: str | None = Field(default=None, exclude=True)
    branch_code: int | None = Field(default=None, exclude=True)
    is_running: bool = Field(default=False, exclude=True)
    license_to_date: str | None = Field(default=None, exclude=True)
    state: str | None = Field(default=None, exclude=True)
    revoked_at: str | None = Field(default=None, exclude=True)
    version: int | None = Field(default=None, exclude=True)
    client_id: str | None = Field(default=None, exclude=True)
    claims: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @property
    def module_code(self) -> str:
        return self.module

    @property
    def params(self) -> dict[str, Any]:
        return self.parameters


class EntitlementsContext(RootModel[list[EntitlementContext]]):
    @property
    def entitlements(self) -> list[EntitlementContext]:
        return self.root

    def __iter__(self):
        return iter(self.root)

    def __len__(self) -> int:
        return len(self.root)

    def __getitem__(self, item):
        return self.root[item]


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


def _safe_date_string(raw: Any) -> str | None:
    parsed = parse_date_or_none(raw)
    return parsed.isoformat() if parsed else None


def _normalize_public_license_status(record: dict[str, Any], license_to_date: str | None, cfg: IntegrationSettings) -> str:
    license_payload = record.get("license") if isinstance(record.get("license"), dict) else {}
    raw_status = str(license_payload.get("status") or record.get("state") or "").strip().lower() or None
    if record.get("revoked_at"):
        return "revoked"
    if not bool(record.get("is_running")):
        return "inactive"
    if not is_license_current(license_to_date, True, cfg.license_extension_days):
        return "expired"
    return raw_status or "active"


def _license_warning_message(days_left: int) -> str:
    suffix = "s" if days_left != 1 else ""
    return f"License about to expire in {days_left} day{suffix}"


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


def _build_entitlement_context(record: dict[str, Any], *, scope: _ResolvedEntitlementScope, claims: ApiClientClaims, module_code: str, cfg: IntegrationSettings) -> EntitlementContext:
    license_to_date = _safe_date_string(record.get("license_to_date"))
    public_status = _normalize_public_license_status(record, license_to_date, cfg)
    return EntitlementContext(
        module=module_code,
        license=EntitlementLicenseContext(
            expiration_date=license_to_date,
            status=public_status,
        ),
        parameters=record.get("params") or {},
        effective_config=record.get("effective_config") or {},
        company_id=str(record.get("company_id") or scope.company_id),
        company_code=_to_int_or_none(record.get("company_code")) or scope.company_code,
        company_name=record.get("company_name") or claims.company_name,
        infrastructure_domain=record.get("domain") or scope.domain,
        infrastructure_serial_num=record.get("infrastructure_serial_num") or claims.infrastructure_serial_num,
        branch_code=_to_int_or_none(record.get("branch_code")) or scope.branch_code,
        is_running=bool(record.get("is_running")),
        license_to_date=license_to_date,
        state=record.get("state"),
        revoked_at=record.get("revoked_at"),
        version=_to_int_or_none(record.get("version")),
        client_id=claims.client_id,
        claims=claims.model_dump(),
    )


async def _load_cached_entitlements(
    cache: IntegrationCache,
    scope: _ResolvedEntitlementScope,
    *,
    module_codes: str | Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    codes = _normalize_module_codes(module_codes) or settings.app_module_codes
    records: list[dict[str, Any]] = []
    for code in codes:
        record = await cache.get_entitlement(scope.domain, scope.company_code, code, scope.branch_code)
        if record:
            records.append(record)
    return records


async def require_sync_key(
    request: Request,
    x_sync_key: str | None = Header(default=None, alias="X-Sync-Key"),
    x_sync_timestamp: str | None = Header(default=None, alias="X-Sync-Timestamp"),
    x_sync_signature: str | None = Header(default=None, alias="X-Sync-Signature"),
    cfg: IntegrationSettings = Depends(get_settings),
):
    if not cfg.sync_key:
        _fail(500, "sync_key_missing", "SYNC_KEY is not configured")
    if x_sync_key and x_sync_key != cfg.sync_key:
        _fail(401, "sync_key_invalid", "Invalid X-Sync-Key")
    if x_sync_timestamp and x_sync_signature:
        try:
            timestamp_value = int(x_sync_timestamp)
        except Exception:
            _fail(401, "sync_signature_invalid", "Invalid sync timestamp")
        now_ts = int(datetime.utcnow().timestamp())
        if abs(now_ts - timestamp_value) > 300:
            _fail(401, "sync_signature_expired", "Sync signature timestamp is too old")
        body = await request.body()
        digest = hmac.new(cfg.sync_key.encode("utf-8"), x_sync_timestamp.encode("utf-8") + b"." + body, hashlib.sha256)
        if not hmac.compare_digest(digest.hexdigest(), x_sync_signature):
            _fail(401, "sync_signature_invalid", "Invalid sync signature")
    elif x_sync_key != cfg.sync_key:
        _fail(401, "sync_key_invalid", "Invalid X-Sync-Key")


async def _require_module_entitlement(
    request: Request,
    claims: ApiClientClaims,
    cache: IntegrationCache,
    cfg: IntegrationSettings,
    *,
    module_code: str,
) -> EntitlementContext:
    token_module = claims.module_code
    if token_module and token_module not in {module_code, "*"}:
        _fail(403, "token_module_mismatch", "Token is not valid for this module")
    scope = await _resolve_entitlement_scope(request, claims, cache)
    record = await cache.get_entitlement(scope.domain, scope.company_code, module_code, scope.branch_code)
    if not record:
        _fail(403, "license_not_found", "No cached effective config found for company/module/branch")
    if not bool(record.get("is_running")):
        _fail(403, "license_not_running", "License is not running")
    if not is_license_current(record.get("license_to_date"), True, cfg.license_extension_days):
        _fail(403, "license_expired", "License has expired")
    if cfg.require_module_params and not (record.get("params") or {}):
        _fail(403, "params_not_found", "Parameters not found for this module")
    license_date = parse_date_or_none(record.get("license_to_date"))
    if license_date:
        days_left = (license_date - datetime.utcnow().date()).days
        if 0 <= days_left <= cfg.license_expiry_warning_days:
            request.state.integration_message = _license_warning_message(days_left)
    return _build_entitlement_context(record, scope=scope, claims=claims, module_code=module_code, cfg=cfg)


async def require_module_entitlement(
    request: Request,
    claims: ApiClientClaims = Depends(require_valid_api_client_token),
    cache: IntegrationCache = Depends(get_cache),
    cfg: IntegrationSettings = Depends(get_settings),
) -> EntitlementContext:
    return await _require_module_entitlement(request, claims, cache, cfg, module_code=cfg.app_module_code)


def require_module_entitlement_for(module_code: str):
    async def _dep(
        request: Request,
        claims: ApiClientClaims = Depends(require_valid_api_client_token),
        cache: IntegrationCache = Depends(get_cache),
        cfg: IntegrationSettings = Depends(get_settings),
    ) -> EntitlementContext:
        return await _require_module_entitlement(request, claims, cache, cfg, module_code=module_code)

    return _dep


async def require_module_parameters(
    entitlement: EntitlementContext = Depends(require_module_entitlement),
) -> dict[str, Any]:
    return entitlement.parameters


def require_module_parameters_for(module_code: str):
    async def _dep(
        entitlement: EntitlementContext = Depends(require_module_entitlement_for(module_code)),
    ) -> dict[str, Any]:
        return entitlement.parameters

    return _dep


async def require_module_entitlements(
    request: Request,
    claims: ApiClientClaims = Depends(require_valid_api_client_token),
    cache: IntegrationCache = Depends(get_cache),
    cfg: IntegrationSettings = Depends(get_settings),
) -> EntitlementsContext:
    scope = await _resolve_entitlement_scope(request, claims, cache)
    records = await _load_cached_entitlements(cache, scope, module_codes=cfg.app_module_codes)
    return EntitlementsContext([
        _build_entitlement_context(record, scope=scope, claims=claims, module_code=str(record.get("module_code")), cfg=cfg)
        for record in records
    ])


async def require_all_module_entitlements(
    request: Request,
    claims: ApiClientClaims = Depends(require_valid_api_client_token),
    cache: IntegrationCache = Depends(get_cache),
    cfg: IntegrationSettings = Depends(get_settings),
) -> EntitlementsContext:
    scope = await _resolve_entitlement_scope(request, claims, cache)
    records = await cache.list_entitlements(company_code=scope.company_code, domain=scope.domain)
    company_level = [record for record in records if record.get("branch_code") in (None, "", 0)]
    return EntitlementsContext([
        _build_entitlement_context(record, scope=scope, claims=claims, module_code=str(record.get("module_code")), cfg=cfg)
        for record in company_level
    ])


def require_module_entitlements_for(module_codes: str | Sequence[str]):
    async def _dep(
        request: Request,
        claims: ApiClientClaims = Depends(require_valid_api_client_token),
        cache: IntegrationCache = Depends(get_cache),
        cfg: IntegrationSettings = Depends(get_settings),
    ) -> EntitlementsContext:
        scope = await _resolve_entitlement_scope(request, claims, cache)
        records = await _load_cached_entitlements(cache, scope, module_codes=module_codes)
        return EntitlementsContext([
            _build_entitlement_context(record, scope=scope, claims=claims, module_code=str(record.get("module_code")), cfg=cfg)
            for record in records
        ])

    return _dep


async def perform_full_sync(
    *,
    cache: IntegrationCache | None = None,
    admin_client: AdminPanelClient | None = None,
) -> dict[str, Any]:
    cache_client = cache or _cache
    api_client = admin_client or _admin_client
    if not settings.admin_panel_client_id or not settings.admin_panel_client_secret:
        raise httpx.HTTPError("ADMIN_PANEL_CLIENT_ID and ADMIN_PANEL_CLIENT_SECRET are required for bootstrap")
    payload = await bootstrap_and_cache_client(
        settings.admin_panel_client_id,
        settings.admin_panel_client_secret,
        cache=cache_client,
        admin_client=api_client,
    )
    return {
        "records": payload.get("records", 0),
        "module_code": settings.app_module_code,
        "module_codes": settings.app_module_codes,
        "cache": payload.get("cache", {}),
        "sync_cursor": payload.get("sync_cursor") or payload.get("cache", {}).get("cursor") or 0,
    }


async def startup_integration() -> None:
    await _cache.connect()
    if settings.sync_on_startup:
        try:
            result = await perform_full_sync()
            logger.info("Initial bootstrap sync complete: %s", result)
        except httpx.HTTPError as exc:
            logger.warning("Initial bootstrap sync skipped (admin panel unavailable): %s", exc)
        except Exception as exc:
            logger.warning("Initial bootstrap sync failed: %s", exc)


async def shutdown_integration() -> None:
    try:
        await _cache.disconnect()
    except Exception as exc:
        logger.warning("Error while closing integration Redis client: %s", exc)


async def get_effective_config(client_key: str | None, module_code: str, *, branch_code: int | None = None) -> dict[str, Any] | None:
    session = await _cache.get_client_session(client_key) if client_key else None
    if not session:
        return None
    record = await _cache.get_entitlement(session.get("infrastructure_domain"), session.get("company_code"), module_code, branch_code)
    return record.get("effective_config") if record else None


async def get_parameters(client_key: str | None, module_code: str, *, branch_code: int | None = None) -> dict[str, Any]:
    config = await get_effective_config(client_key, module_code, branch_code=branch_code)
    if not isinstance(config, dict):
        return {}
    return config.get("parameters") or {}


async def validate_license(client_key: str | None, module_code: str, *, branch_code: int | None = None) -> bool:
    session = await _cache.get_client_session(client_key) if client_key else None
    if not session:
        return False
    record = await _cache.get_entitlement(session.get("infrastructure_domain"), session.get("company_code"), module_code, branch_code)
    if not record:
        return False
    return bool(record.get("is_running")) and is_license_current(record.get("license_to_date"), True, settings.license_extension_days)
