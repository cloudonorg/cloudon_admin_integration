import copy
import json
from datetime import date, datetime, timedelta
from collections.abc import Sequence
from typing import Any

import redis.asyncio as redis

from cloudon_admin_integration.config import IntegrationSettings


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def parse_date_or_none(raw: Any) -> date | None:
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, date):
        return raw
    value = str(raw).strip()
    if not value:
        return None
    value = value[:10]
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class IntegrationCache:
    def __init__(self, cfg: IntegrationSettings):
        self.cfg = cfg
        self.redis: redis.Redis | None = None

    @staticmethod
    def _norm_code(value: Any, *, default: str | None = None) -> str | None:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        return text

    @staticmethod
    def _normalize_codes(value: str | Sequence[str] | None) -> tuple[str, ...]:
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

    def _key(self, domain: str | None, company_code: str | int | None, module_code: str) -> str:
        norm_domain = self._norm_code(domain, default="unknown") or "unknown"
        norm_company = self._norm_code(company_code, default="unknown") or "unknown"
        norm_module = self._norm_code(module_code, default="unknown") or "unknown"
        return f"{self.cfg.redis_key_prefix}:entitlement:{norm_domain}:{norm_company}:{norm_module}"

    @property
    def _index_key(self) -> str:
        return f"{self.cfg.redis_key_prefix}:keys"

    @property
    def _session_index_key(self) -> str:
        return f"{self.cfg.redis_key_prefix}:client_sessions"

    def _session_key(self, client_id: str) -> str:
        return f"{self.cfg.redis_key_prefix}:session:{client_id}"

    async def connect(self) -> None:
        self.redis = redis.Redis(
            host=self.cfg.redis_host,
            port=self.cfg.redis_port,
            db=self.cfg.redis_db,
            password=self.cfg.redis_password,
            decode_responses=True,
        )
        await self.redis.ping()

    async def disconnect(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()
        self.redis = None

    def _ensure(self) -> redis.Redis:
        if self.redis is None:
            raise RuntimeError("Redis client is not connected")
        return self.redis

    async def _get_record_by_key(self, key: str) -> dict[str, Any] | None:
        redis_conn = self._ensure()
        raw = await redis_conn.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        data["_cache_key"] = key
        return data

    async def get_client_session(self, client_id: str) -> dict[str, Any] | None:
        redis_conn = self._ensure()
        raw = await redis_conn.get(self._session_key(client_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None

    async def store_client_session(self, session: dict[str, Any]) -> dict[str, Any]:
        redis_conn = self._ensure()
        client_id = self._norm_code(session.get("client_id"))
        if not client_id:
            raise ValueError("client_id is required")
        record = dict(session)
        record["updated_at"] = utc_now_iso()
        await redis_conn.set(self._session_key(client_id), json.dumps(record))
        await redis_conn.sadd(self._session_index_key, self._session_key(client_id))
        return record

    async def get_entitlement(
        self,
        domain: str | None,
        company_code: str | int | None,
        module_code: str,
        branch_code: str | int | None = None,
    ) -> dict[str, Any] | None:
        key = self._key(domain, company_code, module_code)
        record = await self._get_record_by_key(key)
        if not record:
            return None
        branches = (record.get("params") or {}).get("branches") if isinstance(record.get("params"), dict) else None
        norm_branch = self._norm_code(branch_code)
        if norm_branch is not None and isinstance(branches, list):
            matched = None
            for branch in branches:
                if not isinstance(branch, dict):
                    continue
                branch_value = self._norm_code(branch.get("branch_code"))
                if branch_value == norm_branch:
                    matched = branch
                    break
            if matched is None:
                return None
            record["_matched_branch"] = norm_branch
            record["_selected_branch"] = matched
        else:
            record["_matched_branch"] = None
        return record

    async def upsert_license(
        self,
        domain: str | None,
        company_code: int | str,
        module_code: str,
        *,
        company_id: str | None = None,
        infrastructure_id: str | None = None,
        infrastructure_serial_num: str | None = None,
        company_name: str | None = None,
        infrastructure_domain: str | None = None,
        module_name: str | None = None,
        is_running: bool,
        license_to_date: str | None,
        license: dict[str, Any] | None = None,
        state: str | None,
        revoked_at: str | None,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        redis_conn = self._ensure()
        key = self._key(domain or infrastructure_domain, company_code, module_code)
        existing = await self._get_record_by_key(key) or {}
        company_data = dict(existing.get("company") or {})
        infrastructure_data = dict(existing.get("infrastructure") or {})
        if company_id is not None:
            company_data["id"] = company_id
        if company_code is not None:
            company_data["code"] = int(company_code) if str(company_code).isdigit() else company_code
        if company_name is not None:
            company_data["name"] = company_name
        if infrastructure_id is not None:
            infrastructure_data["id"] = infrastructure_id
        if infrastructure_serial_num is not None:
            infrastructure_data["serial_num"] = infrastructure_serial_num
        if domain is not None or infrastructure_domain is not None:
            infrastructure_data["domain"] = domain or infrastructure_domain
        record = {
            "company": company_data or {
                "id": company_id,
                "code": company_code,
                "name": company_name,
            },
            "infrastructure": infrastructure_data or {
                "id": infrastructure_id,
                "serial_num": infrastructure_serial_num,
                "domain": domain or infrastructure_domain,
            },
            "company_id": company_id or existing.get("company_id"),
            "company_code": int(company_code) if str(company_code).isdigit() else company_code,
            "domain": domain or infrastructure_domain or existing.get("domain"),
            "infrastructure_id": infrastructure_id or existing.get("infrastructure_id"),
            "infrastructure_serial_num": infrastructure_serial_num or existing.get("infrastructure_serial_num"),
            "module_code": module_code,
            "module_name": module_name or existing.get("module_name"),
            "is_running": bool(is_running),
            "license_to_date": license_to_date,
            "license": license or existing.get("license") or {},
            "state": state,
            "revoked_at": revoked_at,
            "params": existing.get("params", {}),
            "updated_at": utc_now_iso(),
            "source": source,
            "metadata": metadata or existing.get("metadata", {}),
        }
        await redis_conn.set(key, json.dumps(record))
        await redis_conn.sadd(self._index_key, key)
        return record

    async def upsert_params(
        self,
        domain: str | None,
        company_code: int | str,
        module_code: str,
        params: dict[str, Any],
        *,
        company_id: str | None = None,
        infrastructure_id: str | None = None,
        infrastructure_serial_num: str | None = None,
        company_name: str | None = None,
        infrastructure_domain: str | None = None,
        module_name: str | None = None,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        redis_conn = self._ensure()
        key = self._key(domain or infrastructure_domain, company_code, module_code)
        company_data = {
            "id": company_id,
            "code": int(company_code) if str(company_code).isdigit() else company_code,
            "name": company_name,
        }
        infrastructure_data = {
            "id": infrastructure_id,
            "serial_num": infrastructure_serial_num,
            "domain": domain or infrastructure_domain,
        }
        existing = await self._get_record_by_key(key) or {
            "company": company_data,
            "infrastructure": infrastructure_data,
            "company_id": company_id,
            "company_code": int(company_code) if str(company_code).isdigit() else company_code,
            "domain": domain or infrastructure_domain,
            "infrastructure_id": infrastructure_id,
            "infrastructure_serial_num": infrastructure_serial_num,
            "module_code": module_code,
            "module_name": module_name,
            "is_running": False,
            "license_to_date": None,
            "license": {},
            "state": None,
            "revoked_at": None,
        }
        existing_company = dict(existing.get("company") or {})
        existing_infrastructure = dict(existing.get("infrastructure") or {})
        if company_id is not None:
            existing_company["id"] = company_id
        if company_code is not None:
            existing_company["code"] = int(company_code) if str(company_code).isdigit() else company_code
        if company_name is not None:
            existing_company["name"] = company_name
        if infrastructure_id is not None:
            existing_infrastructure["id"] = infrastructure_id
        if infrastructure_serial_num is not None:
            existing_infrastructure["serial_num"] = infrastructure_serial_num
        if domain is not None or infrastructure_domain is not None:
            existing_infrastructure["domain"] = domain or infrastructure_domain
        if module_name is not None:
            existing["module_name"] = module_name
        existing["company"] = existing_company or company_data
        existing["infrastructure"] = existing_infrastructure or infrastructure_data
        existing["company_id"] = company_id or existing.get("company_id")
        existing["company_code"] = int(company_code) if str(company_code).isdigit() else company_code
        existing["domain"] = domain or infrastructure_domain or existing.get("domain")
        existing["infrastructure_id"] = infrastructure_id or existing.get("infrastructure_id")
        existing["infrastructure_serial_num"] = infrastructure_serial_num or existing.get("infrastructure_serial_num")
        existing["params"] = copy.deepcopy(params or {})
        existing["updated_at"] = utc_now_iso()
        existing["source"] = source
        existing["metadata"] = metadata or existing.get("metadata", {})
        existing.pop("_cache_key", None)
        existing.pop("_matched_branch", None)
        existing.pop("_selected_branch", None)
        await redis_conn.set(key, json.dumps(existing))
        await redis_conn.sadd(self._index_key, key)
        return existing

    async def delete_entitlement(
        self,
        domain: str | None,
        company_code: str | int,
        module_code: str,
    ) -> int:
        redis_conn = self._ensure()
        key = self._key(domain, company_code, module_code)
        deleted = await redis_conn.delete(key)
        await redis_conn.srem(self._index_key, key)
        return deleted

    @staticmethod
    def _record_matches_scope(
        record: dict[str, Any],
        *,
        company_id: str | None = None,
        company_code: str | int | None = None,
        domain: str | None = None,
    ) -> bool:
        checks = []
        if company_id is not None:
            checks.append(str(record.get("company_id")) == str(company_id))
        if company_code is not None:
            checks.append(str(record.get("company_code")) == str(company_code))
        if domain is not None:
            record_domain = record.get("domain") or record.get("infrastructure", {}).get("domain")
            checks.append(str(record_domain) == str(domain))
        return any(checks) if checks else False

    @staticmethod
    def _session_matches_scope(
        session: dict[str, Any],
        *,
        company_id: str | None = None,
        company_code: str | int | None = None,
        domain: str | None = None,
    ) -> bool:
        checks = []
        if company_id is not None:
            checks.append(str(session.get("company_id")) == str(company_id))
        if company_code is not None:
            checks.append(str(session.get("company_code")) == str(company_code))
        if domain is not None:
            checks.append(str(session.get("infrastructure_domain")) == str(domain))
        return any(checks) if checks else False

    async def update_company_metadata(
        self,
        *,
        company_id: str | None = None,
        company_code: str | int | None = None,
        company_name: str | None = None,
        infrastructure_id: str | None = None,
        infrastructure_serial_num: str | None = None,
        infrastructure_domain: str | None = None,
        source: str = "company_sync",
    ) -> dict[str, int]:
        redis_conn = self._ensure()
        keys = sorted(await redis_conn.smembers(self._index_key))
        updated_records = 0
        updated_sessions = 0
        pipe = redis_conn.pipeline(transaction=False)

        for key in keys:
            record = await self._get_record_by_key(key)
            if not record or not self._record_matches_scope(
                record,
                company_id=company_id,
                company_code=company_code,
                domain=infrastructure_domain,
            ):
                continue
            normalized = dict(record)
            company = dict(normalized.get("company") or {})
            infrastructure = dict(normalized.get("infrastructure") or {})
            if company_id is not None:
                company["id"] = company_id
                normalized["company_id"] = company_id
            if company_code is not None:
                company["code"] = int(company_code) if str(company_code).isdigit() else company_code
                normalized["company_code"] = company["code"]
            if company_name is not None:
                company["name"] = company_name
                normalized["company_name"] = company_name
            if infrastructure_id is not None:
                infrastructure["id"] = infrastructure_id
                normalized["infrastructure_id"] = infrastructure_id
            if infrastructure_serial_num is not None:
                infrastructure["serial_num"] = infrastructure_serial_num
                normalized["infrastructure_serial_num"] = infrastructure_serial_num
            if infrastructure_domain is not None:
                infrastructure["domain"] = infrastructure_domain
                normalized["domain"] = infrastructure_domain
            normalized["company"] = company
            normalized["infrastructure"] = infrastructure
            normalized["updated_at"] = utc_now_iso()
            normalized["source"] = source
            normalized.pop("_cache_key", None)
            normalized.pop("_matched_branch", None)
            normalized.pop("_selected_branch", None)
            pipe.set(key, json.dumps(normalized))
            updated_records += 1

        session_keys = sorted(await redis_conn.smembers(self._session_index_key))
        for session_key in session_keys:
            raw = await redis_conn.get(session_key)
            if not raw:
                continue
            session = json.loads(raw)
            if not isinstance(session, dict):
                continue
            if not self._session_matches_scope(
                session,
                company_id=company_id,
                company_code=company_code,
                domain=infrastructure_domain,
            ):
                continue
            normalized = dict(session)
            if company_id is not None:
                normalized["company_id"] = company_id
            if company_code is not None:
                normalized["company_code"] = int(company_code) if str(company_code).isdigit() else company_code
            if company_name is not None:
                normalized["company_name"] = company_name
            if infrastructure_id is not None:
                normalized["infrastructure_id"] = infrastructure_id
            if infrastructure_serial_num is not None:
                normalized["infrastructure_serial_num"] = infrastructure_serial_num
            if infrastructure_domain is not None:
                normalized["infrastructure_domain"] = infrastructure_domain
            normalized["updated_at"] = utc_now_iso()
            pipe.set(session_key, json.dumps(normalized))
            updated_sessions += 1

        if updated_records or updated_sessions:
            await pipe.execute()
        return {"updated_records": updated_records, "updated_sessions": updated_sessions}

    async def delete_company_records(
        self,
        *,
        company_id: str | None = None,
        company_code: str | int | None = None,
        infrastructure_domain: str | None = None,
    ) -> dict[str, int]:
        redis_conn = self._ensure()
        keys = sorted(await redis_conn.smembers(self._index_key))
        deleted_records = 0
        deleted_sessions = 0
        pipe = redis_conn.pipeline(transaction=False)

        for key in keys:
            record = await self._get_record_by_key(key)
            if not record or not self._record_matches_scope(
                record,
                company_id=company_id,
                company_code=company_code,
                domain=infrastructure_domain,
            ):
                continue
            pipe.delete(key)
            pipe.srem(self._index_key, key)
            deleted_records += 1

        session_keys = sorted(await redis_conn.smembers(self._session_index_key))
        for session_key in session_keys:
            raw = await redis_conn.get(session_key)
            if not raw:
                continue
            session = json.loads(raw)
            if not isinstance(session, dict):
                continue
            if not self._session_matches_scope(
                session,
                company_id=company_id,
                company_code=company_code,
                domain=infrastructure_domain,
            ):
                continue
            pipe.delete(session_key)
            pipe.srem(self._session_index_key, session_key)
            deleted_sessions += 1

        if deleted_records or deleted_sessions:
            await pipe.execute()
        return {"deleted_records": deleted_records, "deleted_sessions": deleted_sessions}

    async def upsert_module_metadata(
        self,
        module_code: str,
        *,
        module_name: str | None = None,
        source: str = "module_sync",
    ) -> dict[str, int]:
        redis_conn = self._ensure()
        keys = sorted(await redis_conn.smembers(self._index_key))
        updated_records = 0
        pipe = redis_conn.pipeline(transaction=False)

        for key in keys:
            record = await self._get_record_by_key(key)
            if not record or str(record.get("module_code")) != str(module_code):
                continue
            normalized = dict(record)
            if module_name is not None:
                normalized["module_name"] = module_name
            normalized["updated_at"] = utc_now_iso()
            normalized["source"] = source
            normalized.pop("_cache_key", None)
            normalized.pop("_matched_branch", None)
            normalized.pop("_selected_branch", None)
            pipe.set(key, json.dumps(normalized))
            updated_records += 1

        if updated_records:
            await pipe.execute()
        return {"updated_records": updated_records}

    async def delete_module_records(self, module_code: str) -> dict[str, int]:
        redis_conn = self._ensure()
        keys = sorted(await redis_conn.smembers(self._index_key))
        deleted_records = 0
        pipe = redis_conn.pipeline(transaction=False)

        for key in keys:
            record = await self._get_record_by_key(key)
            if not record or str(record.get("module_code")) != str(module_code):
                continue
            pipe.delete(key)
            pipe.srem(self._index_key, key)
            deleted_records += 1

        if deleted_records:
            await pipe.execute()
        return {"deleted_records": deleted_records}

    async def clear_params(
        self,
        domain: str | None,
        company_code: str | int,
        module_code: str,
        *,
        source: str = "module_settings_delete",
    ) -> dict[str, int]:
        redis_conn = self._ensure()
        key = self._key(domain, company_code, module_code)
        record = await self._get_record_by_key(key)
        if not record:
            return {"updated_records": 0}
        normalized = dict(record)
        normalized["params"] = {}
        normalized["updated_at"] = utc_now_iso()
        normalized["source"] = source
        normalized.pop("_cache_key", None)
        normalized.pop("_matched_branch", None)
        normalized.pop("_selected_branch", None)
        await redis_conn.set(key, json.dumps(normalized))
        await redis_conn.sadd(self._index_key, key)
        return {"updated_records": 1}

    async def rebuild(
        self,
        records: list[dict[str, Any]],
        client_session: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        redis_conn = self._ensure()
        existing_keys = await redis_conn.smembers(self._index_key)
        if existing_keys:
            await redis_conn.delete(*list(existing_keys))
        await redis_conn.delete(self._index_key)
        if client_session:
            await self.store_client_session(client_session)

        if records:
            pipe = redis_conn.pipeline(transaction=False)
            for record in records:
                key = self._key(
                    record.get("domain") or record.get("infrastructure", {}).get("domain"),
                    record.get("company_code"),
                    record.get("module_code"),
                )
                normalized = dict(record)
                normalized["company_code"] = (
                    int(normalized["company_code"])
                    if str(normalized.get("company_code", "")).isdigit()
                    else normalized.get("company_code")
                )
                pipe.set(key, json.dumps(normalized))
                pipe.sadd(self._index_key, key)
            await pipe.execute()

        return {
            "deleted": len(existing_keys),
            "written": len(records),
            "client_sessions_written": 1 if client_session else 0,
        }

    async def dump(
        self,
        company_id: str | None = None,
        company_code: int | str | None = None,
        module_code: str | Sequence[str] | None = None,
        branch_code: str | int | None = None,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        redis_conn = self._ensure()
        keys = sorted(await redis_conn.smembers(self._index_key))
        module_codes = self._normalize_codes(module_code)
        module_code_set = set(module_codes)
        out: list[dict[str, Any]] = []
        for key in keys:
            record = await self._get_record_by_key(key)
            if not record:
                continue
            if domain and str(record.get("domain") or record.get("infrastructure", {}).get("domain")) != str(domain):
                continue
            if company_id and str(record.get("company_id")) != str(company_id):
                continue
            if company_code is not None and str(record.get("company_code")) != str(company_code):
                continue
            if module_code_set and record.get("module_code") not in module_code_set:
                continue
            if branch_code is not None:
                params = record.get("params") or {}
                branches = params.get("branches") if isinstance(params, dict) else []
                if isinstance(branches, list) and branches:
                    matched = False
                    for branch in branches:
                        if not isinstance(branch, dict):
                            continue
                        if str(branch.get("branch_code")) == str(branch_code):
                            matched = True
                            break
                    if not matched:
                        continue
            out.append(record)
        return out


def is_license_current(license_to_date: str | None, is_running: bool, extension_days: int = 0) -> bool:
    if not is_running:
        return False
    parsed = parse_date_or_none(license_to_date)
    if parsed is None:
        return True
    grace_threshold = date.today() - timedelta(days=max(0, int(extension_days)))
    return parsed > grace_threshold
