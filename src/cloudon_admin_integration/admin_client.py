from __future__ import annotations

from typing import Any

import httpx

from cloudon_admin_integration.config import IntegrationSettings


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "content", "licenses", "params"):
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


def _extract_id_and_code(payload: Any) -> tuple[str | None, str | None]:
    if isinstance(payload, dict):
        raw_id = payload.get("id") or payload.get("company_id")
        raw_code = payload.get("code") or payload.get("company_code")
        return _norm(raw_id), _norm(raw_code)
    return _norm(payload), None


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


def _safe_date(raw: Any) -> str | None:
    text = _norm(raw)
    if not text:
        return None
    return text[:10]


class AdminPanelClient:
    def __init__(self, cfg: IntegrationSettings):
        self.cfg = cfg

    def _allowed_module_codes(self) -> set[str]:
        codes = self.cfg.app_module_codes or (self.cfg.app_module_code,)
        return {code for code in codes if code}

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        timeout = httpx.Timeout(self.cfg.http_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                self.cfg.admin_url(path),
                json=json_body,
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

    async def bootstrap_client_bundle(self) -> dict[str, Any]:
        if not self.cfg.admin_panel_client_id or not self.cfg.admin_panel_client_secret:
            raise httpx.HTTPError("ADMIN_PANEL_CLIENT_ID and ADMIN_PANEL_CLIENT_SECRET are required for bootstrap")
        payload = {
            "client_id": self.cfg.admin_panel_client_id,
            "client_secret": self.cfg.admin_panel_client_secret,
        }
        data = await self._request_json(
            "POST",
            self.cfg.admin_panel_client_bootstrap_path,
            json_body=payload,
        )
        if not isinstance(data, dict):
            raise httpx.HTTPError("Unexpected bootstrap response shape")
        return data

    def normalize_bootstrap_bundle(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        company = payload.get("company") if isinstance(payload.get("company"), dict) else {}
        infrastructure = payload.get("infrastructure") if isinstance(payload.get("infrastructure"), dict) else {}
        modules = _extract_items(payload.get("modules"))

        company_id = _norm(company.get("id") or payload.get("company_id"))
        company_code = _to_int_or_none(company.get("code") or payload.get("company_code"))
        domain = _norm(infrastructure.get("domain") or payload.get("infrastructure_domain"))
        serial_num = _norm(infrastructure.get("serial_num") or payload.get("infrastructure_serial_num"))
        infrastructure_id = _norm(infrastructure.get("id") or payload.get("infrastructure_id"))

        records: list[dict[str, Any]] = []
        allowed_module_codes = self._allowed_module_codes()
        for item in modules:
            module_code = _norm(item.get("module") or item.get("module_code"))
            if not module_code:
                continue
            if (not self.cfg.cache_all_modules) and module_code not in allowed_module_codes:
                continue

            license_payload = item.get("license") if isinstance(item.get("license"), dict) else {}
            parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
            records.append(
                {
                    "company": {
                        "id": company_id,
                        "code": company_code,
                        "name": company.get("name"),
                        "title": company.get("title"),
                    },
                    "infrastructure": {
                        "id": infrastructure_id,
                        "serial_num": serial_num,
                        "domain": domain,
                        "name": infrastructure.get("name"),
                    },
                    "company_id": company_id,
                    "company_code": company_code,
                    "module_code": module_code,
                    "module_id": _norm(item.get("module_id")),
                    "module_name": _norm(item.get("module_name")) or module_code,
                    "domain": domain,
                    "infrastructure_id": infrastructure_id,
                    "infrastructure_serial_num": serial_num,
                    "is_running": (license_payload.get("status") or "").lower() == "active",
                    "license_to_date": _safe_date(
                        license_payload.get("expiration_date")
                        or license_payload.get("license_to_date")
                        or license_payload.get("to_date")
                    ),
                    "license": {
                        "id": _norm(license_payload.get("id")),
                        "from_date": _safe_date(license_payload.get("from_date")),
                        "expiration_date": _safe_date(
                            license_payload.get("expiration_date")
                            or license_payload.get("license_to_date")
                            or license_payload.get("to_date")
                        ),
                        "state": _norm(license_payload.get("state")),
                        "status": _norm(license_payload.get("status")),
                        "revoked_at": _norm(license_payload.get("revoked_at")),
                        "hash": _norm(license_payload.get("hash")),
                        "number_of_users": license_payload.get("number_of_users"),
                    },
                    "state": _norm(license_payload.get("state")),
                    "revoked_at": _norm(license_payload.get("revoked_at")),
                    "params": parameters,
                    "updated_at": payload.get("generated_at") or "",
                    "source": "bootstrap",
                    "metadata": {"from": "admin_panel_bootstrap"},
                }
            )

        client_session = {
            "client_id": _norm(payload.get("client_id")),
            "access": _norm(payload.get("access")),
            "token_type": _norm(payload.get("token_type")) or "Bearer",
            "expires_at": _norm(payload.get("expires_at")),
            "expires_in": payload.get("expires_in"),
            "company_id": company_id,
            "company_code": company_code,
            "company_name": company.get("name"),
            "infrastructure_id": infrastructure_id,
            "infrastructure_serial_num": serial_num,
            "infrastructure_domain": domain,
            "module_code": _norm(payload.get("module_code")),
            "branch_code": _norm(payload.get("branch_code")),
            "updated_at": payload.get("generated_at") or "",
        }
        return records, client_session
