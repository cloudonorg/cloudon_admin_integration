import unittest
from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from cloudon_admin_integration.admin_client import AdminPanelClient
from cloudon_admin_integration.config import IntegrationSettings
from cloudon_admin_integration.dependencies import (
    _build_module_parameters_payload,
    _require_header_module_entitlement,
    _require_header_module_parameters_payload,
    _require_module_parameters_payload,
)
from cloudon_admin_integration.responses import normalize_response_payload
from cloudon_admin_integration.security import ApiClientClaims


class _Request:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.state = type("State", (), {})()


class _Settings:
    license_extension_days = 0
    require_module_params = False
    license_expiry_warning_days = 10


class _Cache:
    def __init__(self, records):
        self.records = list(records)

    async def get_client_session(self, client_id):
        return None

    async def get_entitlement(self, domain, company_code, module_code, branch_code=None):
        for record in self.records:
            if str(record.get("domain")) != str(domain):
                continue
            if str(record.get("company_code")) != str(company_code):
                continue
            if str(record.get("module_code")) != str(module_code):
                continue
            record_branch = record.get("branch_code")
            branches = (record.get("params") or {}).get("branches") if isinstance(record.get("params"), dict) else []
            if branch_code is not None and any(
                isinstance(branch, dict) and str(branch.get("branch_code")) == str(branch_code)
                for branch in (branches or [])
            ):
                return record
            if branch_code is not None and str(record_branch) == str(branch_code):
                return record
            if branch_code is None and record_branch in (None, "", 0, "0"):
                return record
        return None

    async def list_entitlements(self, **filters):
        rows = []
        for record in self.records:
            if filters.get("company_code") is not None and str(record.get("company_code")) != str(
                filters["company_code"]
            ):
                continue
            if filters.get("domain") is not None and str(record.get("domain")) != str(filters["domain"]):
                continue
            if filters.get("module_code") is not None and str(record.get("module_code")) != str(
                filters["module_code"]
            ):
                continue
            rows.append(record)
        return rows


def _effective_record(branch_code, params, **overrides):
    record = {
        "company_id": "company-1",
        "company_code": 10,
        "domain": "pocyfuse",
        "module_code": "sinopsis",
        "branch_code": branch_code,
        "params": params,
        "is_running": True,
        "license_to_date": (date.today() + timedelta(days=30)).isoformat(),
        "version": 1,
    }
    record.update(overrides)
    return record


def _claims():
    return ApiClientClaims(
        token_type="api_client",
        company_id="company-1",
        company_code=10,
        infrastructure_domain="pocyfuse",
    )


class AdminPanelClientNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.client = AdminPanelClient(
            IntegrationSettings(
                app_module_code="pharmacy_one",
                app_module_codes=("pharmacy_one",),
                admin_panel_base_url="https://admin.example.com",
                admin_panel_client_bootstrap_path="/api/client-auth/bootstrap/",
                admin_panel_client_token_path="/api/client-auth/token/",
                admin_panel_effective_config_resolve_path="/api/client-auth/effective-configs/resolve/",
                admin_panel_effective_config_reconcile_path="/api/client-auth/effective-configs/reconcile/",
                admin_panel_system_log_ingest_path="/api/system-logs/ingest/",
                admin_panel_system_log_ingest_bulk_path="/api/system-logs/ingest-bulk/",
                admin_panel_client_id="client-id",
                admin_panel_client_secret="secret",
                http_timeout_seconds=5,
                sync_on_startup=True,
                sync_key="sync-key",
                redis_host="localhost",
                redis_port=6379,
                redis_db=0,
                redis_password=None,
                redis_key_prefix="test:integration",
                admin_panel_jwt_algorithm="HS256",
                admin_panel_jwt_signing_key=None,
                admin_panel_jwt_public_key=None,
                admin_panel_jwt_audience=None,
                enforce_token_module_match=True,
                license_extension_days=0,
                integration_wrap_responses=True,
                integration_excluded_paths=("/docs",),
                require_module_params=False,
                license_expiry_warning_days=10,
                cache_stale_after_seconds=3600,
            )
        )

    def test_normalize_bootstrap_bundle_prefers_effective_configs(self):
        payload = {
            "client_id": "client-id",
            "access": "token",
            "company_id": "company-1",
            "company_code": "2001",
            "company_name": "Test Company",
            "infrastructure": {"domain": "demo"},
            "sync_cursor": 42,
            "effective_configs": [
                {
                    "company_id": "company-1",
                    "company_code": 2001,
                    "company_name": "Test Company",
                    "application_id": "application-1",
                    "application_status": "RUNNING",
                    "application_expires_at": "2026-12-31",
                    "infrastructure_domain": "demo",
                    "module_code": "pharmacy_one",
                    "module_name": "Pharmacy One",
                    "branch_code": 10,
                    "branch_id": "branch-10",
                    "branch_name": "Main",
                    "license_valid_to": "2026-12-31",
                    "license_state": "ACTIVE",
                    "license_status": "active",
                    "active": True,
                    "parameters": {"api_user": "main"},
                    "version": 42,
                    "updated_at": "2026-04-24T08:00:00Z",
                    "deleted": False,
                }
            ],
        }

        records, session = self.client.normalize_bootstrap_bundle(payload)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["module_code"], "pharmacy_one")
        self.assertEqual(records[0]["application_id"], "application-1")
        self.assertEqual(records[0]["application_status"], "RUNNING")
        self.assertEqual(records[0]["metadata"]["application_id"], "application-1")
        self.assertEqual(records[0]["branch_code"], 10)
        self.assertEqual(records[0]["params"]["api_user"], "main")
        self.assertEqual(records[0]["version"], 42)
        self.assertEqual(session["sync_cursor"], 42)


class IntegrationSettingsTests(unittest.TestCase):
    def test_from_env_uses_fixed_admin_endpoint_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = IntegrationSettings.from_env()

        self.assertEqual(settings.admin_panel_client_bootstrap_path, "/api/client-auth/bootstrap/")
        self.assertEqual(
            settings.admin_panel_effective_config_resolve_path,
            "/api/client-auth/effective-configs/resolve/",
        )
        self.assertEqual(
            settings.admin_panel_effective_config_reconcile_path,
            "/api/client-auth/effective-configs/reconcile/",
        )
        self.assertFalse(settings.sync_on_startup)


class ResponseEnvelopeTests(unittest.TestCase):
    def test_structured_http_error_detail_keeps_reason_and_message(self):
        payload = {
            "detail": {
                "reason": "license_not_found",
                "message": "No cached effective config found for company/module/branch",
            }
        }

        self.assertEqual(
            normalize_response_payload(payload, 403),
            {
                "success": False,
                "error": "license_not_found",
                "message": "No cached effective config found for company/module/branch",
                "data": None,
            },
        )

    def test_existing_failure_envelope_keeps_message(self):
        payload = {
            "success": False,
            "error": "license_expired",
            "message": "License has expired",
            "data": {"ignored": True},
        }

        self.assertEqual(
            normalize_response_payload(payload, 403),
            {
                "success": False,
                "error": "license_expired",
                "message": "License has expired",
                "data": None,
            },
        )

    def test_top_level_error_payload_keeps_message(self):
        payload = {
            "error": "admin_panel_unavailable",
            "message": "Could not reach admin panel",
        }

        self.assertEqual(
            normalize_response_payload(payload, 502),
            {
                "success": False,
                "error": "admin_panel_unavailable",
                "message": "Could not reach admin panel",
                "data": None,
            },
        )


class ModuleParameterPayloadTests(unittest.TestCase):
    def test_aggregate_record_keeps_master_and_branch_payload(self):
        root = {
            "module_code": "retail_zoom",
            "branch_code": None,
            "params": {
                "mode": "BRANCHES",
                "master": {"enabled": True},
                "branches": [
                    {
                        "branch_code": 101,
                        "branch_name": "Main Branch",
                        "softone_branch": 1001,
                    }
                ],
            },
            "version": 12,
        }

        self.assertEqual(_build_module_parameters_payload(root, [root]), root["params"])

    def test_branch_records_are_returned_with_master_payload(self):
        root = {
            "module_code": "sinopsis",
            "branch_code": None,
            "params": {"api_url": "https://example.test"},
            "version": 10,
        }
        branch = {
            "module_code": "sinopsis",
            "branch_id": "branch-101",
            "branch_code": 101,
            "branch_name": "Main Branch",
            "params": {"live": True},
            "version": 11,
        }

        self.assertEqual(
            _build_module_parameters_payload(root, [root, branch]),
            {
                "mode": "BRANCHES",
                "master": {"api_url": "https://example.test"},
                "branches": [
                    {
                        "live": True,
                        "branch_id": "branch-101",
                        "branch_code": 101,
                        "branch_name": "Main Branch",
                    }
                ],
            },
        )

    def test_company_only_record_keeps_flat_parameters(self):
        root = {
            "module_code": "pharmacy_one",
            "branch_code": None,
            "params": {"enabled": True},
            "version": 10,
        }

        self.assertEqual(_build_module_parameters_payload(root, [root]), {"enabled": True})


class ModuleParameterDependencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_only_scope_returns_all_module_parameters(self):
        root = _effective_record(None, {})
        branch = _effective_record(
            101,
            {"live": True},
            branch_id="branch-101",
            branch_name="Main Branch",
            version=2,
        )

        with patch("cloudon_admin_integration.dependencies._reconcile_scope_cache", new=AsyncMock()):
            params = await _require_module_parameters_payload(
                _Request(),
                _claims(),
                _Cache([root, branch]),
                _Settings(),
                module_code="sinopsis",
            )

        self.assertEqual(
            params,
            {
                "mode": "BRANCHES",
                "master": {},
                "branches": [
                    {
                        "live": True,
                        "branch_id": "branch-101",
                        "branch_code": 101,
                        "branch_name": "Main Branch",
                    }
                ],
            },
        )

    async def test_branch_scope_keeps_branch_parameters_only(self):
        root = _effective_record(None, {"root": True})
        branch = _effective_record(101, {"live": True}, branch_id="branch-101", branch_name="Main Branch")

        params = await _require_module_parameters_payload(
            _Request(headers={"X-Branch-Code": "101"}),
            _claims(),
            _Cache([root, branch]),
            _Settings(),
            module_code="sinopsis",
        )

        self.assertEqual(params, {"live": True})

    async def test_branch_scope_reads_branch_from_aggregate_record(self):
        aggregate = _effective_record(
            None,
            {
                "mode": "BRANCHES",
                "master": {"default_days": 30},
                "branches": [
                    {
                        "branch_code": 101,
                        "branch_id": "branch-101",
                        "branch_name": "Main Branch",
                        "live": True,
                    }
                ],
            },
        )

        params = await _require_module_parameters_payload(
            _Request(headers={"X-Branch-Code": "101"}),
            _claims(),
            _Cache([aggregate]),
            _Settings(),
            module_code="sinopsis",
        )

        self.assertEqual(
            params,
            {
                "branch_code": 101,
                "branch_id": "branch-101",
                "branch_name": "Main Branch",
                "live": True,
            },
        )

    async def test_header_scope_reads_parameters_without_bearer_token(self):
        aggregate = _effective_record(
            None,
            {
                "mode": "BRANCHES",
                "master": {"default_days": 30},
                "branches": [{"branch_code": 101, "retail_zoom_enabled": True}],
            },
            module_code="retail_zoom",
        )

        params = await _require_header_module_parameters_payload(
            _Request(headers={"domain": "pocyfuse", "company": "10", "branch": "101"}),
            _Cache([aggregate]),
            _Settings(),
            module_code="retail_zoom",
        )

        self.assertEqual(params, {"branch_code": 101, "retail_zoom_enabled": True})

    async def test_header_scope_falls_back_to_company_refresh_when_branch_refresh_404s(self):
        refreshed = _effective_record(
            None,
            {
                "mode": "BRANCHES",
                "master": {"enabled": True},
                "branches": [{"branch_code": 101, "retail_zoom_enabled": True}],
            },
            module_code="retail_zoom",
        )
        branch_404 = httpx.HTTPStatusError(
            "not found",
            request=httpx.Request("POST", "https://admin.example.com/api/client-auth/effective-configs/resolve/"),
            response=httpx.Response(404),
        )

        with patch(
            "cloudon_admin_integration.dependencies.settings",
            SimpleNamespace(admin_panel_client_id="svc-client", admin_panel_client_secret="svc-secret"),
        ), patch(
            "cloudon_admin_integration.dependencies.refresh_effective_config",
            new=AsyncMock(side_effect=[branch_404, refreshed]),
        ) as refresh_mock:
            entitlement = await _require_header_module_entitlement(
                _Request(headers={"domain": "pocyfuse", "company": "10", "branch": "101"}),
                _Cache([]),
                _Settings(),
                module_code="retail_zoom",
            )

        self.assertEqual(refresh_mock.await_count, 2)
        first_call = refresh_mock.await_args_list[0]
        second_call = refresh_mock.await_args_list[1]
        self.assertEqual(first_call.args[0], "retail_zoom")
        self.assertEqual(first_call.kwargs["branch_code"], 101)
        self.assertEqual(second_call.kwargs["branch_code"], None)
        self.assertEqual(entitlement.company_code, 10)
        self.assertEqual(entitlement.branch_code, 101)


if __name__ == "__main__":
    unittest.main()


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of redis-py the cache uses."""

    def __init__(self):
        self.strings = {}
        self.sets = {}
        self.get_calls = 0
        self.mget_calls = 0

    async def get(self, key):
        self.get_calls += 1
        return self.strings.get(key)

    async def set(self, key, value):
        self.strings[key] = value

    async def delete(self, key):
        return 1 if self.strings.pop(key, None) is not None else 0

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def mget(self, keys):
        self.mget_calls += 1
        return [self.strings.get(k) for k in keys]


def _cache_with_fake_redis(prefix="cloudon:integration", fallbacks=()):
    from cloudon_admin_integration.cache import IntegrationCache

    cfg = SimpleNamespace(
        redis_key_prefix=prefix,
        redis_key_prefix_fallbacks=tuple(fallbacks),
        cache_stale_after_seconds=3600,
    )
    cache = IntegrationCache(cfg)
    cache.redis = _FakeRedis()
    return cache


def _record(*, company_code=2001, module_code="pharmacy_one", domain="sync-tenant", version=1, **extra):
    record = {
        "company_code": company_code,
        "module_code": module_code,
        "domain": domain,
        "version": version,
        "branch_code": None,
        "params": {},
        "effective_config": {},
        "is_running": True,
        "license_to_date": "2030-01-01",
    }
    record.update(extra)
    return record


class TenantScopeIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Headers must not redirect a single-tenant token at another company (I2)."""

    async def _resolve(self, headers, claims):
        from cloudon_admin_integration.dependencies import _resolve_entitlement_scope

        return await _resolve_entitlement_scope(_Request(headers), claims, _Cache([]))

    def _claims(self, **extra):
        raw = extra.pop("raw", {})
        return ApiClientClaims(
            token_type="api_client",
            client_id="client-a",
            company_id="company-a",
            company_code=2001,
            infrastructure_domain="tenant-a",
            raw=raw,
            **extra,
        )

    async def test_header_cannot_override_company_code(self):
        with self.assertRaises(Exception) as ctx:
            await self._resolve({"X-Company-Code": "9999"}, self._claims())
        self.assertEqual(ctx.exception.detail["reason"], "company_mismatch")

    async def test_header_cannot_override_domain(self):
        with self.assertRaises(Exception) as ctx:
            await self._resolve({"X-Infrastructure-Domain": "tenant-b"}, self._claims())
        self.assertEqual(ctx.exception.detail["reason"], "domain_mismatch")

    async def test_matching_header_is_accepted(self):
        scope = await self._resolve({"X-Company-Code": "2001"}, self._claims())
        self.assertEqual(scope.company_code, 2001)
        self.assertEqual(scope.domain, "tenant-a")

    async def test_scope_falls_back_to_token_when_no_headers(self):
        scope = await self._resolve({}, self._claims())
        self.assertEqual(scope.company_code, 2001)
        self.assertEqual(scope.domain, "tenant-a")

    async def test_multi_tenant_token_may_select_a_permitted_company(self):
        claims = self._claims(raw={"tenants": [2001, 2002]})
        scope = await self._resolve({"X-Company-Code": "2002"}, claims)
        self.assertEqual(scope.company_code, 2002)

    async def test_multi_tenant_token_rejects_company_outside_allow_list(self):
        claims = self._claims(raw={"tenants": [2001, 2002]})
        with self.assertRaises(Exception) as ctx:
            await self._resolve({"X-Company-Code": "9999"}, claims)
        self.assertEqual(ctx.exception.detail["reason"], "company_not_permitted")


class CacheRebuildPruneTests(unittest.IsolatedAsyncioTestCase):
    """A full bootstrap must drop configs that no longer exist upstream (I5)."""

    async def test_rebuild_prunes_keys_absent_from_the_new_bundle(self):
        cache = _cache_with_fake_redis()
        await cache.upsert_effective_config(_record(module_code="pharmacy_one"))
        await cache.upsert_effective_config(_record(module_code="revoked_module"))
        self.assertEqual(len(await cache.list_entitlements(company_code=2001)), 2)

        result = await cache.rebuild(
            [_record(module_code="pharmacy_one", version=2)],
            client_session={"company_code": 2001, "infrastructure_domain": "sync-tenant", "client_id": "c1"},
        )

        self.assertEqual(result["deleted"], 2)
        remaining = {row["module_code"] for row in await cache.list_entitlements(company_code=2001)}
        self.assertEqual(remaining, {"pharmacy_one"})


class VersionMonotonicityTests(unittest.IsolatedAsyncioTestCase):
    """A retried older delivery must not roll newer state back (I6)."""

    async def test_older_version_is_rejected(self):
        cache = _cache_with_fake_redis()
        await cache.upsert_effective_config(_record(version=10, license_to_date="2030-01-01"))
        await cache.upsert_effective_config(_record(version=5, license_to_date="2020-01-01"))

        stored = await cache.get_entitlement("sync-tenant", 2001, "pharmacy_one")
        self.assertEqual(stored["version"], 10)
        self.assertEqual(stored["license_to_date"], "2030-01-01")
        self.assertEqual(cache.stale_writes_rejected, 1)

    async def test_newer_version_is_applied(self):
        cache = _cache_with_fake_redis()
        await cache.upsert_effective_config(_record(version=10, license_to_date="2030-01-01"))
        await cache.upsert_effective_config(_record(version=11, license_to_date="2031-06-30"))

        stored = await cache.get_entitlement("sync-tenant", 2001, "pharmacy_one")
        self.assertEqual(stored["version"], 11)
        self.assertEqual(stored["license_to_date"], "2031-06-30")
        self.assertEqual(cache.stale_writes_rejected, 0)

    async def test_branch_events_are_not_blocked_by_company_version(self):
        cache = _cache_with_fake_redis()
        await cache.upsert_effective_config(_record(version=10))
        await cache.upsert_effective_config(
            _record(version=3, branch_code=100, params={"api_user": "branch-user"})
        )

        stored = await cache.get_entitlement("sync-tenant", 2001, "pharmacy_one")
        branches = stored["params"]["branches"]
        self.assertEqual([b["branch_code"] for b in branches], [100])
        self.assertEqual(cache.stale_writes_rejected, 0)


class ReadPathTests(unittest.IsolatedAsyncioTestCase):
    """Redis is the read path; the admin panel is only consulted when it has to be (I4)."""

    def _scope(self):
        from cloudon_admin_integration.dependencies import _ResolvedEntitlementScope

        return _ResolvedEntitlementScope(
            client_id="c1",
            company_id="company-a",
            company_code=2001,
            domain="tenant-a",
            branch_code=None,
            session={"client_secret": "s3cret", "client_id": "c1"},
        )

    def _cache_with(self, record):
        class _C:
            async def get_entitlement(self, *a, **k):
                return record

        return _C()

    async def test_fresh_cached_record_skips_the_backend(self):
        from cloudon_admin_integration.dependencies import _get_effective_record

        fresh = _record(version=5)
        fresh["stale_at"] = (
            datetime.now(dt_timezone.utc) + timedelta(hours=1)
        ).isoformat().replace("+00:00", "Z")

        with patch(
            "cloudon_admin_integration.dependencies._refresh_scope_record", new_callable=AsyncMock
        ) as refresh:
            got = await _get_effective_record(self._scope(), "pharmacy_one", cache=self._cache_with(fresh))

        refresh.assert_not_awaited()
        self.assertEqual(got["version"], 5)

    async def test_record_without_stale_at_is_treated_as_fresh(self):
        from cloudon_admin_integration.dependencies import _get_effective_record

        with patch(
            "cloudon_admin_integration.dependencies._refresh_scope_record", new_callable=AsyncMock
        ) as refresh:
            got = await _get_effective_record(
                self._scope(), "pharmacy_one", cache=self._cache_with(_record(version=7))
            )

        refresh.assert_not_awaited()
        self.assertEqual(got["version"], 7)

    async def test_stale_record_triggers_a_refresh(self):
        from cloudon_admin_integration.dependencies import _get_effective_record

        stale = _record(version=1)
        stale["stale_at"] = (
            datetime.now(dt_timezone.utc) - timedelta(hours=1)
        ).isoformat().replace("+00:00", "Z")

        with patch(
            "cloudon_admin_integration.dependencies._refresh_scope_record", new_callable=AsyncMock
        ) as refresh:
            refresh.return_value = _record(version=9)
            got = await _get_effective_record(self._scope(), "pharmacy_one", cache=self._cache_with(stale))

        refresh.assert_awaited_once()
        self.assertEqual(got["version"], 9)

    async def test_missing_record_triggers_a_refresh(self):
        from cloudon_admin_integration.dependencies import _get_effective_record

        with patch(
            "cloudon_admin_integration.dependencies._refresh_scope_record", new_callable=AsyncMock
        ) as refresh:
            refresh.return_value = _record(version=3)
            got = await _get_effective_record(self._scope(), "pharmacy_one", cache=self._cache_with(None))

        refresh.assert_awaited_once()
        self.assertEqual(got["version"], 3)

    async def test_stale_record_is_served_when_the_backend_is_down(self):
        from cloudon_admin_integration.dependencies import _get_effective_record

        stale = _record(version=1)
        stale["stale_at"] = (
            datetime.now(dt_timezone.utc) - timedelta(hours=1)
        ).isoformat().replace("+00:00", "Z")

        with patch(
            "cloudon_admin_integration.dependencies._refresh_scope_record", new_callable=AsyncMock
        ) as refresh:
            refresh.side_effect = httpx.HTTPError("panel unreachable")
            got = await _get_effective_record(self._scope(), "pharmacy_one", cache=self._cache_with(stale))

        # Better to honour an entitlement that was valid an hour ago than to refuse
        # a request the customer has paid for.
        self.assertEqual(got["version"], 1)


class StalenessTimezoneTests(unittest.TestCase):
    """stale_at is compared in UTC regardless of the host's timezone (I8)."""

    def test_future_utc_stamp_is_not_stale(self):
        from cloudon_admin_integration.dependencies import _record_is_stale

        future = (datetime.now(dt_timezone.utc) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        self.assertFalse(_record_is_stale({"stale_at": future}))

    def test_past_utc_stamp_is_stale(self):
        from cloudon_admin_integration.dependencies import _record_is_stale

        past = (datetime.now(dt_timezone.utc) - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        self.assertTrue(_record_is_stale({"stale_at": past}))

    def test_naive_stamp_is_read_as_utc(self):
        from cloudon_admin_integration.dependencies import _record_is_stale

        naive_past = (datetime.now(dt_timezone.utc) - timedelta(hours=2)).replace(tzinfo=None).isoformat()
        self.assertTrue(_record_is_stale({"stale_at": naive_past}))

    def test_missing_stamp_is_not_stale(self):
        from cloudon_admin_integration.dependencies import _record_is_stale

        self.assertFalse(_record_is_stale({}))


class RedisPrefixNormalizationTests(unittest.TestCase):
    """One namespace for the fleet, tolerant of how it is written (I9)."""

    def test_trailing_colon_is_stripped(self):
        from cloudon_admin_integration.config import _normalize_prefix

        self.assertEqual(_normalize_prefix("admin_panel:"), "admin_panel")

    def test_bare_form_is_unchanged(self):
        from cloudon_admin_integration.config import _normalize_prefix

        self.assertEqual(_normalize_prefix("admin_panel"), "admin_panel")

    def test_whitespace_and_repeated_colons_are_trimmed(self):
        from cloudon_admin_integration.config import _normalize_prefix

        self.assertEqual(_normalize_prefix("  admin_panel::  "), "admin_panel")

    def test_blank_becomes_none_so_the_caller_can_default(self):
        from cloudon_admin_integration.config import _normalize_prefix

        self.assertIsNone(_normalize_prefix("   "))
        self.assertIsNone(_normalize_prefix(":"))
        self.assertIsNone(_normalize_prefix(None))

    def test_key_layout_has_no_doubled_separator(self):
        from cloudon_admin_integration.cache import IntegrationCache
        from cloudon_admin_integration.config import _normalize_prefix

        cfg = SimpleNamespace(
            redis_key_prefix=_normalize_prefix("admin_panel:"), cache_stale_after_seconds=3600
        )
        key = IntegrationCache(cfg)._key("tenant-a", 2001, "pharmacy_one")

        self.assertEqual(key, "admin_panel:pharmacy_one:tenant-a:2001")
        self.assertNotIn("::", key)


class ListEntitlementsEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    """Listing reads in one round-trip and skips keys it can rule out (I12)."""

    async def _seeded(self, n_companies=5):
        cache = _cache_with_fake_redis()
        for i in range(n_companies):
            await cache.upsert_effective_config(_record(company_code=2000 + i, module_code="pharmacy_one"))
            await cache.upsert_effective_config(_record(company_code=2000 + i, module_code="rapid_test"))
        cache.redis.get_calls = 0
        cache.redis.mget_calls = 0
        return cache

    async def test_listing_uses_a_single_mget(self):
        cache = await self._seeded()

        rows = await cache.list_entitlements()

        self.assertEqual(len(rows), 10)
        self.assertEqual(cache.redis.mget_calls, 1)
        self.assertEqual(cache.redis.get_calls, 0)

    async def test_company_filter_narrows_before_fetching(self):
        cache = await self._seeded()

        rows = await cache.list_entitlements(company_code=2003)

        self.assertEqual({r["company_code"] for r in rows}, {2003})
        self.assertEqual(len(rows), 2)

    async def test_module_filter_narrows_before_fetching(self):
        cache = await self._seeded()

        rows = await cache.list_entitlements(module_code="rapid_test")

        self.assertEqual({r["module_code"] for r in rows}, {"rapid_test"})
        self.assertEqual(len(rows), 5)

    async def test_combined_filters_return_one_row(self):
        cache = await self._seeded()

        rows = await cache.list_entitlements(company_code=2001, module_code="pharmacy_one", domain="sync-tenant")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company_code"], 2001)

    async def test_domain_mismatch_returns_nothing(self):
        cache = await self._seeded()

        self.assertEqual(await cache.list_entitlements(domain="other-tenant"), [])

    async def test_empty_cache_makes_no_calls(self):
        cache = _cache_with_fake_redis()

        self.assertEqual(await cache.list_entitlements(), [])
        self.assertEqual(cache.redis.mget_calls, 0)


class ResponseEnvelopeHeaderTests(unittest.TestCase):
    """The envelope must not lose what the route set (I13)."""

    def _app(self):
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse, StreamingResponse
        from cloudon_admin_integration.responses import wire_response_envelope

        app = FastAPI()
        wire_response_envelope(app, excluded_paths={"/raw"})

        @app.get("/with-headers")
        def with_headers():
            return JSONResponse(
                {"value": 1},
                headers={"X-Total-Count": "42", "Set-Cookie": "session=abc; Path=/"},
            )

        @app.get("/stream")
        def stream():
            def gen():
                yield b'{"a":1}\n'
                yield b'{"a":2}\n'
            return StreamingResponse(gen(), media_type="application/x-ndjson")

        @app.get("/plain")
        def plain():
            return {"ok": True}

        return app

    def _client(self):
        from fastapi.testclient import TestClient

        return TestClient(self._app())

    def test_route_headers_survive_the_envelope(self):
        r = self._client().get("/with-headers")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["data"], {"value": 1})
        # Previously the envelope built a fresh response and dropped these.
        self.assertEqual(r.headers.get("x-total-count"), "42")
        self.assertIn("session=abc", r.headers.get("set-cookie", ""))

    def test_content_length_is_not_carried_from_the_original_body(self):
        r = self._client().get("/with-headers")

        self.assertEqual(int(r.headers["content-length"]), len(r.content))

    def test_streaming_responses_pass_through_unwrapped(self):
        r = self._client().get("/stream")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, '{"a":1}\n{"a":2}\n')

    def test_plain_json_is_still_wrapped(self):
        body = self._client().get("/plain").json()

        self.assertTrue(body["success"])
        self.assertEqual(body["data"], {"ok": True})


class PrefixCutoverTests(unittest.IsolatedAsyncioTestCase):
    """Moving to `cloudon:admin_panel` must not blind a service mid-flight.

    A live service is restarted onto the new namespace before the panel has
    resynced into it, so for a while the only copy of a licence is the one under
    the old prefix. Reads fall back to it; writes, prunes and deletes stay on the
    primary so the old namespace only ever shrinks.
    """

    OLD = "cloudon:integration"
    NEW = "cloudon:admin_panel"

    def _both(self):
        return _cache_with_fake_redis(prefix=self.NEW, fallbacks=(self.OLD,))

    async def _seed_old(self, cache, **record):
        old = _cache_with_fake_redis(prefix=self.OLD)
        old.redis = cache.redis
        return await old.upsert_effective_config(_record(**record))

    async def test_a_licence_only_in_the_old_namespace_is_still_served(self):
        cache = self._both()
        await self._seed_old(cache, version=4)

        stored = await cache.get_entitlement("sync-tenant", 2001, "pharmacy_one")

        self.assertIsNotNone(stored)
        self.assertEqual(stored["version"], 4)

    async def test_the_new_namespace_wins_once_the_resync_lands(self):
        cache = self._both()
        await self._seed_old(cache, version=4, license_to_date="2020-01-01")
        await cache.upsert_effective_config(_record(version=5, license_to_date="2031-01-01"))

        stored = await cache.get_entitlement("sync-tenant", 2001, "pharmacy_one")

        self.assertEqual(stored["license_to_date"], "2031-01-01")

    async def test_writes_never_touch_the_old_namespace(self):
        cache = self._both()
        await cache.upsert_effective_config(_record(version=5))

        written = set(cache.redis.strings)
        self.assertTrue(all(key.startswith(self.NEW) for key in written), written)

    async def test_a_company_in_both_namespaces_is_listed_once(self):
        cache = self._both()
        await self._seed_old(cache, version=4)
        await cache.upsert_effective_config(_record(version=5))

        rows = await cache.list_entitlements(company_code=2001)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["version"], 5)

    async def test_a_revoked_licence_is_dropped_from_both_namespaces(self):
        cache = self._both()
        await self._seed_old(cache, version=4)
        await cache.upsert_effective_config(_record(version=5))

        await cache.delete_effective_config("sync-tenant", 2001, "pharmacy_one")

        self.assertIsNone(await cache.get_entitlement("sync-tenant", 2001, "pharmacy_one"))
