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

    async def _request_paginated(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        timeout = httpx.Timeout(self.cfg.http_timeout_seconds)
        out: list[dict[str, Any]] = []
        next_url = self.cfg.admin_url(path)
        async with httpx.AsyncClient(timeout=timeout) as client:
            while next_url:
                response = await client.get(next_url, headers=headers)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, list):
                    out.extend([x for x in payload if isinstance(x, dict)])
                    break
                if isinstance(payload, dict):
                    out.extend(_extract_items(payload))
                    raw_next = payload.get("next")
                    next_url = raw_next if isinstance(raw_next, str) and raw_next else None
                    continue
                break
        return out

    async def _get_admin_user_token(self) -> str:
        if not self.cfg.django_api_user or not self.cfg.django_api_password:
            raise httpx.HTTPError("DJANGO_API_USER and DJANGO_API_PASSWORD are required for full sync")
        payload = {
            "username": self.cfg.django_api_user,
            "password": self.cfg.django_api_password,
        }
        data = await self._request_json("POST", self.cfg.admin_panel_authorization_path, json_body=payload)
        if not isinstance(data, dict) or not data.get("access"):
            raise httpx.HTTPError("Could not obtain admin JWT from /api/authorization/")
        return str(data["access"])

    async def fetch_full_sync_payload(self) -> dict[str, Any]:
        token = await self._get_admin_user_token()
        headers = {"Authorization": f"Bearer {token}"}
        running_licenses = await self._request_paginated(self.cfg.admin_panel_running_licenses_path, headers=headers)
        module_settings = await self._request_paginated(self.cfg.admin_panel_module_settings_path, headers=headers)
        modules = await self._request_paginated(self.cfg.admin_panel_modules_path, headers=headers)
        companies = await self._request_paginated(self.cfg.admin_panel_companies_path, headers=headers)
        company_api_clients = await self._request_paginated(
            self.cfg.admin_panel_company_api_clients_path,
            headers=headers,
        )
        return {
            "running_licenses": running_licenses,
            "module_settings": module_settings,
            "modules": modules,
            "companies": companies,
            "company_api_clients": company_api_clients,
        }

    async def proxy_client_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self._request_json(
            "POST",
            self.cfg.admin_panel_client_token_path,
            json_body=payload,
        )
        if not isinstance(data, dict):
            raise httpx.HTTPError("Unexpected token response shape")
        return data

    def _build_id_code_map(self, items: list[dict[str, Any]], *, code_fields: tuple[str, ...]) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in items:
            code = None
            for field in code_fields:
                code = _norm(item.get(field))
                if code:
                    break
            if not code:
                continue
            for id_field in ("id", "pk", "module_id", "company_id"):
                raw_id = item.get(id_field)
                if raw_id is not None:
                    out[str(raw_id)] = code
        return out

    def normalize_full_sync_records(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        modules = _extract_items(payload.get("modules"))
        companies = _extract_items(payload.get("companies"))
        licenses = _extract_items(payload.get("running_licenses"))
        settings = _extract_items(payload.get("module_settings"))

        module_map = self._build_id_code_map(modules, code_fields=("module_code", "code", "name"))
        company_map = self._build_id_code_map(companies, code_fields=("company_code", "code", "name"))
        company_code_to_ids: dict[str, set[str]] = {}
        for comp in companies:
            comp_id, comp_code = _extract_id_and_code(comp)
            if comp_id and comp_code:
                company_code_to_ids.setdefault(comp_code, set()).add(comp_id)

        records: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        for lic in licenses:
            module_code = _norm(lic.get("module_code")) or module_map.get(str(lic.get("module_id")))
            company_id, company_code_nested = _extract_id_and_code(lic.get("company"))
            company_id = company_id or _norm(lic.get("company_id")) or _norm(lic.get("company"))
            company_code = (
                _norm(lic.get("company_code"))
                or company_code_nested
                or company_map.get(str(lic.get("company_id")))
            )
            if not company_id and company_code:
                ids = sorted(company_code_to_ids.get(company_code, set()))
                if len(ids) == 1:
                    company_id = ids[0]
            branch_code = _norm(lic.get("branch_code"))
            if not module_code or not company_id:
                continue
            if (not self.cfg.cache_all_modules) and module_code != self.cfg.app_module_code:
                continue

            state = _norm(lic.get("state"))
            revoked_at = _norm(lic.get("revoked_at"))
            to_date = _safe_date(lic.get("to_date") or lic.get("license_to_date"))
            is_running = str(state or "").lower() in {"running", "active", "enabled", "1", "true"}
            if revoked_at:
                is_running = False

            key = (company_id, module_code, branch_code)
            records[key] = {
                "company_id": company_id,
                "company_code": _to_int_or_none(company_code),
                "module_code": module_code,
                "branch_code": _to_int_or_none(branch_code),
                "is_running": is_running,
                "license_to_date": to_date,
                "state": state,
                "revoked_at": revoked_at,
                "params": {},
                "source": "full_sync",
                "updated_at": "",
                "metadata": {"from": "running_licenses"},
            }

        for item in settings:
            module_ref = item.get("module_id")
            if module_ref is None:
                module_ref = item.get("module")
            module_code = _norm(item.get("module_code")) or module_map.get(str(module_ref))
            company_id, company_code_nested = _extract_id_and_code(item.get("company"))
            company_id = company_id or _norm(item.get("company_id")) or _norm(item.get("company"))
            company_code = (
                _norm(item.get("company_code"))
                or company_code_nested
                or company_map.get(str(item.get("company_id")))
            )
            if not company_id and company_code:
                ids = sorted(company_code_to_ids.get(company_code, set()))
                if len(ids) == 1:
                    company_id = ids[0]
            branch_code = _norm(item.get("branch_code"))
            if not module_code or not company_id:
                continue
            if (not self.cfg.cache_all_modules) and module_code != self.cfg.app_module_code:
                continue

            key = (company_id, module_code, branch_code)
            record = records.setdefault(
                key,
                {
                    "company_id": company_id,
                    "company_code": _to_int_or_none(company_code),
                    "module_code": module_code,
                    "branch_code": _to_int_or_none(branch_code),
                    "is_running": False,
                    "license_to_date": None,
                    "state": None,
                    "revoked_at": None,
                    "params": {},
                    "source": "full_sync",
                    "updated_at": "",
                    "metadata": {"from": "module_settings_only"},
                },
            )

            if isinstance(item.get("data"), dict):
                record["params"].update(item["data"])
                continue

            if isinstance(item.get("params"), dict):
                record["params"].update(item["params"])
                continue

            p_key = _norm(item.get("param_key") or item.get("key") or item.get("name"))
            if p_key:
                record["params"][p_key] = item.get("param_value", item.get("value"))

        return list(records.values())

    def normalize_client_mappings(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        companies = _extract_items(payload.get("companies"))
        company_api_clients = _extract_items(payload.get("company_api_clients"))

        code_by_id: dict[str, str] = {}
        for comp in companies:
            comp_id, comp_code = _extract_id_and_code(comp)
            if comp_id and comp_code:
                code_by_id[comp_id] = comp_code

        mappings: list[dict[str, str]] = []
        for row in company_api_clients:
            client_id = _norm(row.get("client_id"))
            if not client_id:
                continue
            company_id, _company_code_nested = _extract_id_and_code(row.get("company"))
            company_id = company_id or _norm(row.get("company_id")) or _norm(row.get("company"))
            if not company_id:
                continue
            company_code = code_by_id.get(company_id) or _norm(row.get("company_code"))
            mappings.append(
                {
                    "client_id": client_id,
                    "company_id": company_id,
                    "company_code": _to_int_or_none(company_code),
                }
            )
        return mappings
