import copy
import json
from datetime import date, datetime, timedelta
from collections.abc import Sequence
from typing import Any

import redis.asyncio as redis
from redis.exceptions import RedisError

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


def is_license_current(raw_to_date: Any, require_running: bool = True, extension_days: int = 0) -> bool:
    license_date = parse_date_or_none(raw_to_date)
    if not license_date:
        return not require_running
    today = datetime.utcnow().date() - timedelta(days=max(int(extension_days or 0), 0))
    return license_date >= today


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

    def _key(
        self,
        domain: str | None,
        company_code: str | int | None,
        module_code: str,
        branch_code: str | int | None = None,
    ) -> str:
        norm_domain = self._norm_code(domain, default="unknown") or "unknown"
        norm_company = self._norm_code(company_code, default="unknown") or "unknown"
        norm_module = self._norm_code(module_code, default="unknown") or "unknown"
        return f"{self.cfg.redis_key_prefix}:{norm_module}:{norm_domain}:{norm_company}"

    def _legacy_key(
        self,
        domain: str | None,
        company_code: str | int | None,
        module_code: str,
        branch_code: str | int | None = None,
    ) -> str:
        norm_domain = self._norm_code(domain, default="unknown") or "unknown"
        norm_company = self._norm_code(company_code, default="unknown") or "unknown"
        norm_module = self._norm_code(module_code, default="unknown") or "unknown"
        norm_branch = self._norm_code(branch_code, default="root") or "root"
        return f"{self.cfg.redis_key_prefix}:effective:{norm_domain}:{norm_company}:{norm_module}:{norm_branch}"

    @property
    def _index_key(self) -> str:
        return f"{self.cfg.redis_key_prefix}:keys"

    @property
    def _session_index_key(self) -> str:
        return f"{self.cfg.redis_key_prefix}:client_sessions"

    @property
    def _cursor_key(self) -> str:
        return f"{self.cfg.redis_key_prefix}:sync_cursor"

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
        try:
            await self.redis.ping()
        except RedisError as exc:
            self.redis = None
            raise RuntimeError(
                "Redis unavailable at "
                f"REDIS_HOST={self.cfg.redis_host!r} REDIS_PORT={self.cfg.redis_port}. "
                "When the API runs in Docker, REDIS_HOST must be the Redis service/container "
                "name on the same Docker network, such as 'redis' or 'pharmacyone_redis', "
                "not 'localhost'."
            ) from exc

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
        return data if isinstance(data, dict) else None

    def _is_legacy_key(self, key: str) -> bool:
        return key.startswith(f"{self.cfg.redis_key_prefix}:effective:")

    @staticmethod
    def _is_root_branch(branch_code: Any) -> bool:
        return branch_code in (None, "", 0, "0", "root", "_")

    @staticmethod
    def _branch_code_text(value: Any) -> str | None:
        if value in (None, "", 0, "0", "root", "_"):
            return None
        return str(value).strip() or None

    @classmethod
    def _coerce_parameters_container(cls, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            return {}
        if isinstance(params.get("master"), dict) or isinstance(params.get("branches"), list):
            container = copy.deepcopy(params)
            container["master"] = container.get("master") if isinstance(container.get("master"), dict) else {}
            container["branches"] = container.get("branches") if isinstance(container.get("branches"), list) else []
            container["mode"] = container.get("mode") or "BRANCHES"
            return container
        return {"mode": "BRANCHES", "master": copy.deepcopy(params), "branches": []}

    @classmethod
    def _branch_matches(cls, branch: dict[str, Any], branch_code: Any) -> bool:
        expected = cls._branch_code_text(branch_code)
        actual = cls._branch_code_text(branch.get("branch_code") or branch.get("branch"))
        return expected is not None and actual == expected

    @classmethod
    def _record_has_branch(cls, record: dict[str, Any], branch_code: Any) -> bool:
        expected = cls._branch_code_text(branch_code)
        if expected is None:
            return True
        record_branch = cls._branch_code_text(record.get("branch_code"))
        if record_branch == expected:
            return True
        params = record.get("params")
        branches = params.get("branches") if isinstance(params, dict) and isinstance(params.get("branches"), list) else []
        return any(isinstance(branch, dict) and cls._branch_matches(branch, expected) for branch in branches)

    @classmethod
    def _merge_branch_params(cls, params: Any, record: dict[str, Any]) -> dict[str, Any]:
        branch_code = record.get("branch_code")
        container = cls._coerce_parameters_container(params)
        branch_payload = copy.deepcopy(record.get("params") if isinstance(record.get("params"), dict) else {})
        branch_payload["branch_id"] = record.get("branch_id") or branch_payload.get("branch_id")
        branch_payload["branch_code"] = branch_code
        branch_payload["branch_name"] = record.get("branch_name") or branch_payload.get("branch_name")
        branches = [branch for branch in container.get("branches", []) if isinstance(branch, dict)]
        replaced = False
        for idx, branch in enumerate(branches):
            if cls._branch_matches(branch, branch_code):
                branches[idx] = branch_payload
                replaced = True
                break
        if not replaced:
            branches.append(branch_payload)
        container["branches"] = branches
        container["mode"] = "BRANCHES"
        return container

    @classmethod
    def _remove_branch_params(cls, params: Any, branch_code: Any) -> dict[str, Any]:
        container = cls._coerce_parameters_container(params)
        container["branches"] = [
            branch
            for branch in container.get("branches", [])
            if not (isinstance(branch, dict) and cls._branch_matches(branch, branch_code))
        ]
        return container

    def _aggregate_record(self, record: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
        normalized = dict(record)
        branch_code = normalized.get("branch_code")
        existing = copy.deepcopy(existing or {})
        aggregate = {
            **existing,
            **normalized,
            "branch_code": None,
            "branch_id": None,
            "branch_name": None,
        }
        aggregate["domain"] = normalized.get("domain") or existing.get("domain")
        aggregate["module_code"] = normalized.get("module_code") or existing.get("module_code")
        aggregate["company_code"] = normalized.get("company_code") or existing.get("company_code")
        aggregate["company_id"] = normalized.get("company_id") or existing.get("company_id")
        aggregate["company_name"] = normalized.get("company_name") or existing.get("company_name")
        aggregate["infrastructure_id"] = normalized.get("infrastructure_id") or existing.get("infrastructure_id")
        aggregate["infrastructure_serial_num"] = normalized.get("infrastructure_serial_num") or existing.get("infrastructure_serial_num")
        aggregate["module_name"] = normalized.get("module_name") or existing.get("module_name") or aggregate.get("module_code")
        aggregate["deleted"] = bool(normalized.get("deleted")) if self._is_root_branch(branch_code) else False

        current_params = existing.get("params") if isinstance(existing.get("params"), dict) else {}
        if self._is_root_branch(branch_code):
            incoming_params = normalized.get("params") if isinstance(normalized.get("params"), dict) else {}
            if isinstance(incoming_params.get("branches"), list):
                aggregate["params"] = copy.deepcopy(incoming_params)
            elif isinstance(current_params.get("branches"), list):
                merged = self._coerce_parameters_container(current_params)
                merged["master"] = copy.deepcopy(incoming_params)
                aggregate["params"] = merged
            else:
                aggregate["params"] = copy.deepcopy(incoming_params)
        else:
            aggregate["params"] = self._merge_branch_params(current_params, normalized)

        effective_config = copy.deepcopy(existing.get("effective_config") or {})
        effective_config.update(copy.deepcopy(normalized.get("effective_config") or {}))
        effective_config["branch_code"] = None
        effective_config["branch_id"] = None
        effective_config["branch_name"] = None
        effective_config["parameters"] = copy.deepcopy(aggregate.get("params") or {})
        aggregate["effective_config"] = effective_config
        return aggregate

    async def _delete_legacy_keys(
        self,
        domain: str | None,
        company_code: str | int,
        module_code: str,
        branch_code: str | int | None = None,
    ) -> None:
        redis_conn = self._ensure()
        if branch_code is None:
            prefix = self._legacy_key(domain, company_code, module_code, "")[:-4]
            keys = [key for key in await redis_conn.smembers(self._index_key) if str(key).startswith(prefix)]
            keys.append(self._legacy_key(domain, company_code, module_code, None))
        else:
            keys = [self._legacy_key(domain, company_code, module_code, branch_code)]
        for key in set(keys):
            await redis_conn.delete(key)
            await redis_conn.srem(self._index_key, key)

    async def get_sync_cursor(self) -> int:
        redis_conn = self._ensure()
        raw = await redis_conn.get(self._cursor_key)
        if not raw:
            return 0
        try:
            return int(raw)
        except Exception:
            return 0

    async def set_sync_cursor(self, version: int | None) -> int:
        redis_conn = self._ensure()
        value = int(version or 0)
        await redis_conn.set(self._cursor_key, str(value))
        return value

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
        sync_cursor = record.get("sync_cursor")
        if sync_cursor is not None:
            await self.set_sync_cursor(int(sync_cursor or 0))
        return record

    async def upsert_effective_config(self, record: dict[str, Any]) -> dict[str, Any]:
        redis_conn = self._ensure()
        company_code = record.get("company_code")
        module_code = record.get("module_code")
        branch_code = record.get("branch_code")
        domain = record.get("domain")
        if company_code is None or not module_code:
            raise ValueError("company_code and module_code are required")
        key = self._key(domain, company_code, module_code, branch_code)
        existing = await self._get_record_by_key(key)
        normalized = self._aggregate_record(record, existing)
        normalized["updated_at"] = normalized.get("updated_at") or utc_now_iso()
        normalized["stale_at"] = (
            datetime.utcnow() + timedelta(seconds=max(int(self.cfg.cache_stale_after_seconds or 0), 0))
        ).replace(microsecond=0).isoformat() + "Z"
        await redis_conn.set(key, json.dumps(normalized))
        await redis_conn.sadd(self._index_key, key)
        await self._delete_legacy_keys(domain, company_code, module_code, branch_code)
        await self.set_sync_cursor(max(int(normalized.get("version") or 0), await self.get_sync_cursor()))
        return normalized

    async def delete_effective_config(
        self,
        domain: str | None,
        company_code: str | int,
        module_code: str,
        branch_code: str | int | None = None,
    ) -> int:
        redis_conn = self._ensure()
        key = self._key(domain, company_code, module_code, branch_code)
        if branch_code is not None:
            existing = await self._get_record_by_key(key)
            if existing:
                existing["params"] = self._remove_branch_params(existing.get("params"), branch_code)
                effective_config = copy.deepcopy(existing.get("effective_config") or {})
                effective_config["parameters"] = copy.deepcopy(existing.get("params") or {})
                existing["effective_config"] = effective_config
                existing["updated_at"] = utc_now_iso()
                await redis_conn.set(key, json.dumps(existing))
                await self._delete_legacy_keys(domain, company_code, module_code, branch_code)
                return 1
            legacy_key = self._legacy_key(domain, company_code, module_code, branch_code)
            deleted = await redis_conn.delete(legacy_key)
            await redis_conn.srem(self._index_key, legacy_key)
            return deleted
        deleted = await redis_conn.delete(key)
        await redis_conn.srem(self._index_key, key)
        await self._delete_legacy_keys(domain, company_code, module_code, branch_code)
        return deleted

    async def rebuild(
        self,
        records: list[dict[str, Any]],
        *,
        client_session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        redis_conn = self._ensure()
        company_code = None
        domain = None
        if client_session:
            company_code = client_session.get("company_code")
            domain = client_session.get("infrastructure_domain")
        existing_keys = []
        if company_code is not None:
            existing_keys = [
                key
                for key in sorted(await redis_conn.smembers(self._index_key))
                if f":{company_code}:" in key and (domain is None or f":{domain}:" in key)
            ]
        replaced = 0
        deleted = 0
        for key in existing_keys:
            await redis_conn.delete(key)
            await redis_conn.srem(self._index_key, key)
            deleted += 1
        max_version = 0
        for record in records:
            if record.get("deleted"):
                await self.delete_effective_config(
                    record.get("domain"),
                    record.get("company_code"),
                    record.get("module_code"),
                    record.get("branch_code"),
                )
                deleted += 1
                continue
            await self.upsert_effective_config(record)
            replaced += 1
            max_version = max(max_version, int(record.get("version") or 0))
        if client_session:
            if max_version:
                client_session["sync_cursor"] = max_version
            await self.store_client_session(client_session)
        elif max_version:
            await self.set_sync_cursor(max_version)
        return {"replaced": replaced, "deleted": deleted, "cursor": max_version}

    async def get_entitlement(
        self,
        domain: str | None,
        company_code: str | int | None,
        module_code: str,
        branch_code: str | int | None = None,
    ) -> dict[str, Any] | None:
        current = await self._get_record_by_key(self._key(domain, company_code, module_code, None))
        if current:
            return current
        if branch_code is not None:
            specific = await self._get_record_by_key(self._legacy_key(domain, company_code, module_code, branch_code))
            if specific:
                return specific
        return await self._get_record_by_key(self._legacy_key(domain, company_code, module_code, None))

    async def list_entitlements(
        self,
        *,
        company_id: str | None = None,
        company_code: str | int | None = None,
        module_code: str | Sequence[str] | None = None,
        branch_code: str | int | None = None,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        redis_conn = self._ensure()
        keys = sorted(await redis_conn.smembers(self._index_key))
        module_codes = set(self._normalize_codes(module_code))
        rows: list[dict[str, Any]] = []
        for key in keys:
            record = await self._get_record_by_key(key)
            if not record:
                continue
            if company_id is not None and str(record.get("company_id")) != str(company_id):
                continue
            if company_code is not None and str(record.get("company_code")) != str(company_code):
                continue
            if domain is not None and str(record.get("domain")) != str(domain):
                continue
            if branch_code is not None and not self._record_has_branch(record, branch_code):
                continue
            if module_codes and str(record.get("module_code")) not in module_codes:
                continue
            rows.append(record)
        current_keys = {str(row.get("_cache_key")) for row in rows if not self._is_legacy_key(str(row.get("_cache_key") or ""))}
        if current_keys:
            rows = [
                row
                for row in rows
                if not self._is_legacy_key(str(row.get("_cache_key") or ""))
                or self._key(row.get("domain"), row.get("company_code"), row.get("module_code")) not in current_keys
            ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), str(item.get("module_code") or ""), str(item.get("branch_code") or "")))
        return rows

    async def dump(
        self,
        *,
        company_id: str | None = None,
        company_code: str | int | None = None,
        module_code: str | Sequence[str] | None = None,
        branch_code: str | int | None = None,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.list_entitlements(
            company_id=company_id,
            company_code=company_code,
            module_code=module_code,
            branch_code=branch_code,
            domain=domain,
        )

    async def upsert_license(self, domain, company_code, module_code, **kwargs):
        existing = await self.get_entitlement(domain, company_code, module_code, kwargs.get("branch_code")) or {}
        effective_config = copy.deepcopy(existing.get("effective_config") or {})
        effective_config.update(
            {
                "company_code": company_code,
                "module_code": module_code,
                "license_valid_to": kwargs.get("license_to_date"),
                "license_state": kwargs.get("state"),
                "license_status": (kwargs.get("license") or {}).get("status") or kwargs.get("state"),
                "active": bool(kwargs.get("is_running")),
                "updated_at": utc_now_iso(),
            }
        )
        existing.update(
            {
                "company_id": kwargs.get("company_id") or existing.get("company_id"),
                "company_code": int(company_code) if str(company_code).isdigit() else company_code,
                "company_name": kwargs.get("company_name") or existing.get("company_name"),
                "domain": domain or kwargs.get("infrastructure_domain") or existing.get("domain"),
                "infrastructure_id": kwargs.get("infrastructure_id") or existing.get("infrastructure_id"),
                "infrastructure_serial_num": kwargs.get("infrastructure_serial_num") or existing.get("infrastructure_serial_num"),
                "module_code": module_code,
                "module_name": kwargs.get("module_name") or existing.get("module_name") or module_code,
                "branch_code": kwargs.get("branch_code"),
                "version": int(kwargs.get("version") or existing.get("version") or 0),
                "effective_config": effective_config,
                "params": existing.get("params") or {},
                "is_running": bool(kwargs.get("is_running")),
                "license_to_date": kwargs.get("license_to_date"),
                "license": kwargs.get("license") or existing.get("license") or {},
                "state": kwargs.get("state"),
                "revoked_at": kwargs.get("revoked_at"),
                "deleted": False,
                "source": kwargs.get("source") or "legacy_webhook",
                "metadata": kwargs.get("metadata") or existing.get("metadata") or {},
            }
        )
        return await self.upsert_effective_config(existing)

    async def upsert_params(self, domain, company_code, module_code, params, **kwargs):
        existing = await self.get_entitlement(domain, company_code, module_code, kwargs.get("branch_code")) or {}
        effective_config = copy.deepcopy(existing.get("effective_config") or {})
        effective_config.update(
            {
                "company_code": company_code,
                "module_code": module_code,
                "parameters": params or {},
                "updated_at": utc_now_iso(),
            }
        )
        existing.update(
            {
                "company_id": kwargs.get("company_id") or existing.get("company_id"),
                "company_code": int(company_code) if str(company_code).isdigit() else company_code,
                "company_name": kwargs.get("company_name") or existing.get("company_name"),
                "domain": domain or kwargs.get("infrastructure_domain") or existing.get("domain"),
                "infrastructure_id": kwargs.get("infrastructure_id") or existing.get("infrastructure_id"),
                "infrastructure_serial_num": kwargs.get("infrastructure_serial_num") or existing.get("infrastructure_serial_num"),
                "module_code": module_code,
                "module_name": kwargs.get("module_name") or existing.get("module_name") or module_code,
                "branch_code": kwargs.get("branch_code"),
                "version": int(kwargs.get("version") or existing.get("version") or 0),
                "effective_config": effective_config,
                "params": params or {},
                "deleted": False,
                "source": kwargs.get("source") or "legacy_webhook",
                "metadata": kwargs.get("metadata") or existing.get("metadata") or {},
            }
        )
        return await self.upsert_effective_config(existing)

    async def clear_params(self, domain, company_code, module_code, branch_code=None):
        existing = await self.get_entitlement(domain, company_code, module_code, branch_code)
        if not existing:
            return 0
        if branch_code is not None:
            existing["params"] = self._remove_branch_params(existing.get("params"), branch_code)
        else:
            existing["params"] = {}
        effective_config = copy.deepcopy(existing.get("effective_config") or {})
        effective_config["parameters"] = copy.deepcopy(existing.get("params") or {})
        existing["effective_config"] = effective_config
        await self.upsert_effective_config(existing)
        return 1

    async def delete_module_records(self, module_code: str) -> int:
        redis_conn = self._ensure()
        keys = sorted(await redis_conn.smembers(self._index_key))
        deleted = 0
        for key in keys:
            record = await self._get_record_by_key(key)
            if not record or str(record.get("module_code")) != str(module_code):
                continue
            await redis_conn.delete(key)
            await redis_conn.srem(self._index_key, key)
            deleted += 1
        return deleted

    async def update_company_metadata(self, **kwargs):
        rows = await self.list_entitlements(
            company_id=kwargs.get("company_id"),
            company_code=kwargs.get("company_code"),
            domain=kwargs.get("infrastructure_domain"),
        )
        updated = 0
        for record in rows:
            effective_config = copy.deepcopy(record.get("effective_config") or {})
            for key, payload_key in (
                ("company_name", "company_name"),
                ("infrastructure_id", "infrastructure_id"),
                ("infrastructure_serial_num", "infrastructure_serial_num"),
                ("infrastructure_domain", "domain"),
            ):
                value = kwargs.get(key)
                if value is not None:
                    record[payload_key] = value
                    if payload_key == "domain":
                        effective_config["infrastructure_domain"] = value
                    else:
                        effective_config[payload_key] = value
            record["effective_config"] = effective_config
            await self.upsert_effective_config(record)
            updated += 1
        return {"updated_records": updated, "updated_sessions": 0}

    async def delete_company_records(self, **kwargs):
        rows = await self.list_entitlements(
            company_id=kwargs.get("company_id"),
            company_code=kwargs.get("company_code"),
            domain=kwargs.get("infrastructure_domain"),
        )
        deleted = 0
        for record in rows:
            deleted += await self.delete_effective_config(
                record.get("domain"),
                record.get("company_code"),
                record.get("module_code"),
                record.get("branch_code"),
            )
        return {"deleted_records": deleted, "deleted_sessions": 0}

    async def upsert_module_metadata(self, module_code: str, *, module_name: str | None = None, source: str = "module_sync"):
        rows = await self.list_entitlements(module_code=module_code)
        updated = 0
        for record in rows:
            record["module_name"] = module_name or record.get("module_name")
            effective_config = copy.deepcopy(record.get("effective_config") or {})
            effective_config["module_name"] = module_name or effective_config.get("module_name")
            record["effective_config"] = effective_config
            record["source"] = source
            await self.upsert_effective_config(record)
            updated += 1
        return {"updated_records": updated}
