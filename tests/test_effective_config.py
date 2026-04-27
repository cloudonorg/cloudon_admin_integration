import unittest
from unittest.mock import patch

from cloudon_admin_integration.admin_client import AdminPanelClient
from cloudon_admin_integration.config import IntegrationSettings
from cloudon_admin_integration.responses import normalize_response_payload


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


if __name__ == "__main__":
    unittest.main()
