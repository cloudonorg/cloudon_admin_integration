from __future__ import annotations

from typing import Any

import httpx

from cloudon_admin_integration.config import IntegrationSettings


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("effective_configs", "results", "items", "data", "content", "modules"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return [payload]
    return []


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def _first_present(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item and item.get(key) not in (None, ""):
            return item.get(key)
    return None


def _normal_status(value: Any) -> str | None:
    text = _norm(value)
    return text.lower() if text else None


def _infer_running_status(item: dict[str, Any], license_status: str | None) -> bool:
    if item.get("active") is not None:
        return bool(item.get("active"))
    if item.get("is_running") is not None:
        return bool(item.get("is_running"))
    status = _normal_status(license_status or item.get("state") or item.get("license_state"))
    if item.get("revoked_at") or item.get("license_revoked_at"):
        return False
    return status == "active"


class AdminPanelClient:
    def __init__(self, cfg: IntegrationSettings):
        self.cfg = cfg

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        timeout = httpx.Timeout(self.cfg.http_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                self.cfg.admin_url(path),
                json=json_body,
            )
            response.raise_for_status()
            return response.json()

    async def bootstrap_client_bundle(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        branch_code: str | None = None,
        module_code: str | None = None,
    ) -> dict[str, Any]:
        client_id = (client_id or self.cfg.admin_panel_client_id or "").strip() or None
        client_secret = (client_secret or self.cfg.admin_panel_client_secret or "").strip() or None
        if not client_id or not client_secret:
            raise httpx.HTTPError("client_id and client_secret are required for bootstrap")
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if _norm(branch_code):
            payload["branch_code"] = _norm(branch_code)
        if _norm(module_code):
            payload["module_code"] = _norm(module_code)
        data = await self._request_json("POST", self.cfg.admin_panel_client_bootstrap_path, json_body=payload)
        if not isinstance(data, dict):
            raise httpx.HTTPError("Unexpected bootstrap response shape")
        return data

    async def resolve_effective_config(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        module_code: str,
        branch_code: int | str | None = None,
    ) -> dict[str, Any]:
        client_id = (client_id or self.cfg.admin_panel_client_id or "").strip() or None
        client_secret = (client_secret or self.cfg.admin_panel_client_secret or "").strip() or None
        if not client_id or not client_secret:
            raise httpx.HTTPError("client_id and client_secret are required for effective config resolve")
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "module_code": module_code,
        }
        if branch_code is not None:
            payload["branch_code"] = int(branch_code)
        data = await self._request_json("POST", self.cfg.admin_panel_effective_config_resolve_path, json_body=payload)
        if not isinstance(data, dict):
            raise httpx.HTTPError("Unexpected resolve response shape")
        return data

    async def reconcile_effective_configs(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        since_version: int | None = None,
    ) -> dict[str, Any]:
        client_id = (client_id or self.cfg.admin_panel_client_id or "").strip() or None
        client_secret = (client_secret or self.cfg.admin_panel_client_secret or "").strip() or None
        if not client_id or not client_secret:
            raise httpx.HTTPError("client_id and client_secret are required for reconciliation")
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if since_version is not None:
            payload["since_version"] = int(since_version)
        data = await self._request_json("POST", self.cfg.admin_panel_effective_config_reconcile_path, json_body=payload)
        if not isinstance(data, dict):
            raise httpx.HTTPError("Unexpected reconcile response shape")
        return data

    def _normalize_effective_config(self, item: dict[str, Any]) -> dict[str, Any] | None:
        module_code = _norm(item.get("module_code") or item.get("module"))
        company_code = _to_int_or_none(item.get("company_code"))
        if not module_code or company_code is None:
            return None
        branch_code = _to_int_or_none(item.get("branch_code"))
        effective_config = dict(item)
        license_payload = item.get("license") if isinstance(item.get("license"), dict) else {}
        license_to_date = _norm(
            _first_present(
                item,
                (
                    "license_valid_to",
                    "license_to_date",
                    "to_date",
                    "valid_to",
                    "expiration_date",
                ),
            )
            or _first_present(license_payload, ("expiration_date", "valid_to", "to_date"))
        )
        license_status = _norm(
            _first_present(item, ("license_status", "effective_entitlement", "state", "license_state"))
            or _first_present(license_payload, ("status", "state"))
        )
        parameters = item.get("parameters")
        if parameters is None:
            parameters = item.get("params") or {}
        is_running = _infer_running_status(item, license_status)
        domain = _norm(item.get("infrastructure_domain") or item.get("domain"))
        return {
            "company_id": _norm(item.get("company_id")),
            "company_code": company_code,
            "company_name": _norm(item.get("company_name")),
            "application_id": _norm(item.get("application_id")),
            "application_status": _norm(item.get("application_status")),
            "application_expires_at": _norm(item.get("application_expires_at")),
            "domain": domain,
            "infrastructure_id": _norm(item.get("infrastructure_id")),
            "infrastructure_serial_num": _norm(item.get("infrastructure_serial_num")),
            "module_code": module_code,
            "module_name": _norm(item.get("module_name")) or module_code,
            "branch_code": branch_code,
            "branch_id": _norm(item.get("branch_id")),
            "branch_name": _norm(item.get("branch_name")),
            "version": _to_int_or_none(item.get("version")) or 0,
            "updated_at": _norm(item.get("updated_at")) or "",
            "deleted": bool(item.get("deleted")),
            "effective_config": effective_config,
            "params": parameters or {},
            "is_running": is_running,
            "license_to_date": license_to_date,
            "license": {
                "expiration_date": license_to_date,
                "status": license_status,
                "state": _norm(item.get("license_state") or item.get("state") or license_payload.get("state")),
                "revoked_at": _norm(item.get("license_revoked_at") or item.get("revoked_at") or license_payload.get("revoked_at")),
                "number_of_users": item.get("number_of_users") or license_payload.get("number_of_users"),
            },
            "state": _norm(item.get("license_state") or item.get("state") or license_payload.get("state")),
            "revoked_at": _norm(item.get("license_revoked_at") or item.get("revoked_at") or license_payload.get("revoked_at")),
            "source": "admin_panel_effective_config",
            "metadata": {
                "from": "admin_panel_effective_config",
                "application_id": _norm(item.get("application_id")),
                "application_status": _norm(item.get("application_status")),
                "application_expires_at": _norm(item.get("application_expires_at")),
            },
        }

    def normalize_bootstrap_bundle(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        effective_items = _extract_items(payload.get("effective_configs"))
        if effective_items:
            records = [record for item in effective_items if (record := self._normalize_effective_config(item))]
        else:
            modules = _extract_items(payload.get("modules"))
            company = payload.get("company") if isinstance(payload.get("company"), dict) else {}
            infrastructure = payload.get("infrastructure") if isinstance(payload.get("infrastructure"), dict) else {}
            records = []
            for item in modules:
                module_code = _norm(item.get("module") or item.get("module_code"))
                if not module_code:
                    continue
                license_payload = item.get("license") if isinstance(item.get("license"), dict) else {}
                records.append(
                    {
                        "company_id": _norm(company.get("id") or payload.get("company_id")),
                        "company_code": _to_int_or_none(company.get("code") or payload.get("company_code")),
                        "company_name": _norm(company.get("name") or payload.get("company_name")),
                        "application_id": _norm(item.get("application_id")),
                        "application_status": _norm(item.get("application_status")),
                        "application_expires_at": _norm(item.get("application_expires_at")),
                        "domain": _norm(infrastructure.get("domain") or payload.get("infrastructure_domain")),
                        "infrastructure_id": _norm(infrastructure.get("id") or payload.get("infrastructure_id")),
                        "infrastructure_serial_num": _norm(infrastructure.get("serial_num") or payload.get("infrastructure_serial_num")),
                        "module_code": module_code,
                        "module_name": _norm(item.get("module_name")) or module_code,
                        "branch_code": None,
                        "branch_id": None,
                        "branch_name": None,
                        "version": _to_int_or_none(payload.get("sync_cursor")) or 0,
                        "updated_at": _norm(payload.get("generated_at")) or "",
                        "deleted": False,
                        "effective_config": {
                            "company_id": _norm(company.get("id") or payload.get("company_id")),
                            "company_code": _to_int_or_none(company.get("code") or payload.get("company_code")),
                            "company_name": _norm(company.get("name") or payload.get("company_name")),
                            "infrastructure_domain": _norm(infrastructure.get("domain") or payload.get("infrastructure_domain")),
                            "module_code": module_code,
                            "module_name": _norm(item.get("module_name")) or module_code,
                            "branch_code": None,
                            "parameters": item.get("parameters") or {},
                            "license_valid_to": _norm(license_payload.get("expiration_date")),
                            "license_state": _norm(license_payload.get("state")),
                            "license_status": _norm(license_payload.get("status")),
                            "active": (_norm(license_payload.get("status")) or "").lower() == "active",
                            "version": _to_int_or_none(payload.get("sync_cursor")) or 0,
                            "updated_at": _norm(payload.get("generated_at")) or "",
                            "deleted": False,
                        },
                        "params": item.get("parameters") or {},
                        "is_running": (_norm(license_payload.get("status")) or "").lower() == "active",
                        "license_to_date": _norm(license_payload.get("expiration_date")),
                        "license": {
                            "expiration_date": _norm(license_payload.get("expiration_date")),
                            "status": _norm(license_payload.get("status")),
                            "state": _norm(license_payload.get("state")),
                            "revoked_at": _norm(license_payload.get("revoked_at")),
                            "number_of_users": license_payload.get("number_of_users"),
                        },
                        "state": _norm(license_payload.get("state")),
                        "revoked_at": _norm(license_payload.get("revoked_at")),
                        "source": "bootstrap_legacy_bundle",
                        "metadata": {
                            "from": "admin_panel_bootstrap_legacy_bundle",
                            "application_id": _norm(item.get("application_id")),
                            "application_status": _norm(item.get("application_status")),
                            "application_expires_at": _norm(item.get("application_expires_at")),
                        },
                    }
                )

        client_session = {
            "client_id": _norm(payload.get("client_id")),
            "access": _norm(payload.get("access")),
            "token_type": _norm(payload.get("token_type")) or "Bearer",
            "expires_at": _norm(payload.get("expires_at")),
            "expires_in": payload.get("expires_in"),
            "company_id": _norm(payload.get("company_id") or (payload.get("company") or {}).get("id")),
            "company_code": _to_int_or_none(payload.get("company_code") or (payload.get("company") or {}).get("code")),
            "company_name": _norm(payload.get("company_name") or (payload.get("company") or {}).get("name")),
            "infrastructure_id": _norm(payload.get("infrastructure_id") or (payload.get("infrastructure") or {}).get("id")),
            "infrastructure_serial_num": _norm(payload.get("infrastructure_serial_num") or (payload.get("infrastructure") or {}).get("serial_num")),
            "infrastructure_domain": _norm(payload.get("infrastructure_domain") or (payload.get("infrastructure") or {}).get("domain")),
            "module_code": _norm(payload.get("module_code")),
            "branch_code": _norm(payload.get("branch_code")),
            "updated_at": _norm(payload.get("generated_at")) or "",
            "sync_cursor": _to_int_or_none(payload.get("sync_cursor")) or 0,
        }
        return records, client_session
