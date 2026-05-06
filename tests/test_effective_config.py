import unittest
from datetime import date, timedelta
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
                admin_panel_effective_config_resolve_path="/api/client-auth/effective-configs/resolve/",
                admin_panel_effective_config_reconcile_path="/api/client-auth/effective-configs/reconcile/",
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
