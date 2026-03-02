import json
from datetime import date, datetime, timedelta
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

    def _key(self, company_id: str, module_code: str, branch_code: str | int | None = None) -> str:
        branch = self._norm_code(branch_code, default="__all__") or "__all__"
        return f"{self.cfg.redis_key_prefix}:entitlement:{company_id}:{module_code}:{branch}"

    @property
    def _index_key(self) -> str:
        return f"{self.cfg.redis_key_prefix}:keys"

    @property
    def _client_index_key(self) -> str:
        return f"{self.cfg.redis_key_prefix}:client_keys"

    @property
    def _company_code_index_keys(self) -> str:
        return f"{self.cfg.redis_key_prefix}:company_code_keys"

    def _client_key(self, client_id: str) -> str:
        return f"{self.cfg.redis_key_prefix}:client:{client_id}"

    def _company_code_key(self, company_code: str) -> str:
        return f"{self.cfg.redis_key_prefix}:company_code:{company_code}"

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

    async def get_entitlement(
        self, company_id: str, module_code: str, branch_code: str | int | None = None
    ) -> dict[str, Any] | None:
        norm_branch = self._norm_code(branch_code)
        if norm_branch is not None:
            by_branch = await self._get_record_by_key(self._key(company_id, module_code, norm_branch))
            if by_branch:
                by_branch["_matched_branch"] = norm_branch
                return by_branch
        fallback = await self._get_record_by_key(self._key(company_id, module_code, None))
        if fallback:
            fallback["_matched_branch"] = None
        return fallback

    async def get_company_by_client_id(self, client_id: str) -> dict[str, Any] | None:
        redis_conn = self._ensure()
        raw = await redis_conn.get(self._client_key(client_id))
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None

    async def resolve_company_ids(
        self,
        *,
        company_id: str | None = None,
        company_code: int | str | None = None,
    ) -> list[str]:
        if company_id:
            return [company_id]
        if not company_code:
            return []
        redis_conn = self._ensure()
        ids = await redis_conn.smembers(self._company_code_key(str(company_code)))
        return sorted(str(x) for x in ids)

    async def upsert_license(
        self,
        company_id: str,
        module_code: str,
        branch_code: str | None,
        *,
        company_code: int | str | None,
        is_running: bool,
        license_to_date: str | None,
        state: str | None,
        revoked_at: str | None,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        redis_conn = self._ensure()
        key = self._key(company_id, module_code, branch_code)
        existing = await self._get_record_by_key(key) or {}
        record = {
            "company_id": company_id,
            "company_code": company_code,
            "module_code": module_code,
            "branch_code": branch_code,
            "is_running": bool(is_running),
            "license_to_date": license_to_date,
            "state": state,
            "revoked_at": revoked_at,
            "params": existing.get("params", {}),
            "updated_at": utc_now_iso(),
            "source": source,
            "metadata": metadata or existing.get("metadata", {}),
        }
        await redis_conn.set(key, json.dumps(record))
        await redis_conn.sadd(self._index_key, key)
        if company_code is not None:
            code_key = self._company_code_key(str(company_code))
            await redis_conn.sadd(code_key, company_id)
            await redis_conn.sadd(self._company_code_index_keys, code_key)
        return record

    async def upsert_params(
        self,
        company_id: str,
        module_code: str,
        branch_code: str | None,
        params: dict[str, Any],
        *,
        company_code: int | str | None = None,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        redis_conn = self._ensure()
        key = self._key(company_id, module_code, branch_code)
        existing = await self._get_record_by_key(key) or {
            "company_id": company_id,
            "company_code": company_code,
            "module_code": module_code,
            "branch_code": branch_code,
            "is_running": False,
            "license_to_date": None,
            "state": None,
            "revoked_at": None,
        }
        existing["params"] = params or {}
        existing["updated_at"] = utc_now_iso()
        existing["source"] = source
        existing["metadata"] = metadata or existing.get("metadata", {})
        if company_code is not None:
            existing["company_code"] = company_code
        existing.pop("_cache_key", None)
        existing.pop("_matched_branch", None)
        await redis_conn.set(key, json.dumps(existing))
        await redis_conn.sadd(self._index_key, key)
        code_to_store = existing.get("company_code")
        if code_to_store:
            code_key = self._company_code_key(str(code_to_store))
            await redis_conn.sadd(code_key, company_id)
            await redis_conn.sadd(self._company_code_index_keys, code_key)
        return existing

    async def delete_entitlement(
        self, company_id: str, module_code: str, branch_code: str | None = None
    ) -> int:
        redis_conn = self._ensure()
        key = self._key(company_id, module_code, branch_code)
        deleted = await redis_conn.delete(key)
        await redis_conn.srem(self._index_key, key)
        return deleted

    async def rebuild(
        self,
        records: list[dict[str, Any]],
        client_mappings: list[dict[str, str]] | None = None,
    ) -> dict[str, int]:
        redis_conn = self._ensure()
        existing_keys = await redis_conn.smembers(self._index_key)
        if existing_keys:
            await redis_conn.delete(*list(existing_keys))
        await redis_conn.delete(self._index_key)
        existing_client_keys = await redis_conn.smembers(self._client_index_key)
        if existing_client_keys:
            await redis_conn.delete(*list(existing_client_keys))
        await redis_conn.delete(self._client_index_key)
        existing_code_keys = await redis_conn.smembers(self._company_code_index_keys)
        if existing_code_keys:
            await redis_conn.delete(*list(existing_code_keys))
        await redis_conn.delete(self._company_code_index_keys)

        if records:
            pipe = redis_conn.pipeline(transaction=False)
            for record in records:
                key = self._key(
                    record["company_id"],
                    record["module_code"],
                    record.get("branch_code"),
                )
                pipe.set(key, json.dumps(record))
                pipe.sadd(self._index_key, key)
                if record.get("company_code"):
                    code_key = self._company_code_key(str(record["company_code"]))
                    pipe.sadd(code_key, str(record["company_id"]))
                    pipe.sadd(self._company_code_index_keys, code_key)
            await pipe.execute()

        if client_mappings:
            pipe = redis_conn.pipeline(transaction=False)
            for row in client_mappings:
                client_id = str(row["client_id"])
                key = self._client_key(client_id)
                pipe.set(
                    key,
                    json.dumps(
                        {
                            "client_id": client_id,
                            "company_id": str(row["company_id"]),
                            "company_code": row.get("company_code"),
                            "updated_at": utc_now_iso(),
                        }
                    ),
                )
                pipe.sadd(self._client_index_key, key)
            await pipe.execute()

        return {
            "deleted": len(existing_keys),
            "written": len(records),
            "client_mappings_written": len(client_mappings or []),
        }

    async def dump(
        self,
        company_id: str | None = None,
        company_code: int | str | None = None,
        module_code: str | None = None,
        branch_code: str | None = None,
    ) -> list[dict[str, Any]]:
        redis_conn = self._ensure()
        keys = sorted(await redis_conn.smembers(self._index_key))
        out: list[dict[str, Any]] = []
        for key in keys:
            record = await self._get_record_by_key(key)
            if not record:
                continue
            if company_id and str(record.get("company_id")) != str(company_id):
                continue
            if company_code is not None and str(record.get("company_code")) != str(company_code):
                continue
            if module_code and record.get("module_code") != module_code:
                continue
            if branch_code is not None and record.get("branch_code") != branch_code:
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
