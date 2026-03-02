import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class IntegrationSettings:
    app_module_code: str
    admin_panel_base_url: str
    admin_panel_client_token_path: str
    admin_panel_authorization_path: str
    admin_panel_running_licenses_path: str
    admin_panel_module_settings_path: str
    admin_panel_modules_path: str
    admin_panel_companies_path: str
    admin_panel_company_api_clients_path: str
    django_api_user: str | None
    django_api_password: str | None
    http_timeout_seconds: float
    sync_on_startup: bool
    allow_token_proxy: bool
    sync_key: str | None
    redis_host: str
    redis_port: int
    redis_db: int
    redis_password: str | None
    redis_key_prefix: str
    admin_panel_jwt_algorithm: str
    admin_panel_jwt_signing_key: str | None
    admin_panel_jwt_public_key: str | None
    admin_panel_jwt_audience: str | None
    enforce_token_module_match: bool
    license_extension_days: int
    cache_all_modules: bool

    @classmethod
    def from_env(cls) -> "IntegrationSettings":
        base_url = (os.getenv("DJANGO_API_URL") or "").strip().rstrip("/")
        return cls(
            app_module_code=(os.getenv("APP_MODULE_CODE") or "pharmacy_one").strip(),
            admin_panel_base_url=base_url,
            admin_panel_client_token_path=(
                os.getenv("ADMIN_PANEL_CLIENT_TOKEN_PATH") or "/api/client-auth/token/"
            ).strip(),
            admin_panel_authorization_path=(
                os.getenv("ADMIN_PANEL_AUTHORIZATION_PATH") or "/api/authorization/"
            ).strip(),
            admin_panel_running_licenses_path=(
                os.getenv("ADMIN_PANEL_RUNNING_LICENSES_PATH") or "/api/licenses/running/"
            ).strip(),
            admin_panel_module_settings_path=(
                os.getenv("ADMIN_PANEL_MODULE_SETTINGS_PATH") or "/api/module-settings/"
            ).strip(),
            admin_panel_modules_path=(
                os.getenv("ADMIN_PANEL_MODULES_PATH") or "/api/modules/"
            ).strip(),
            admin_panel_companies_path=(
                os.getenv("ADMIN_PANEL_COMPANIES_PATH") or "/api/companies/"
            ).strip(),
            admin_panel_company_api_clients_path=(
                os.getenv("ADMIN_PANEL_COMPANY_API_CLIENTS_PATH") or "/api/company-api-clients/"
            ).strip(),
            django_api_user=(os.getenv("DJANGO_API_USER") or "").strip() or None,
            django_api_password=(os.getenv("DJANGO_API_PASSWORD") or "").strip() or None,
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS") or 10),
            sync_on_startup=_as_bool(os.getenv("SYNC_ON_STARTUP"), True),
            allow_token_proxy=_as_bool(os.getenv("ALLOW_TOKEN_PROXY"), False),
            sync_key=(os.getenv("SYNC_KEY") or "").strip() or None,
            redis_host=(os.getenv("REDIS_HOST") or "localhost").strip(),
            redis_port=int(os.getenv("REDIS_PORT") or 6379),
            redis_db=int(os.getenv("REDIS_DB") or 0),
            redis_password=(os.getenv("REDIS_PASSWORD") or "").strip() or None,
            redis_key_prefix=(os.getenv("REDIS_KEY_PREFIX") or "pharmacyone:integration").strip(),
            admin_panel_jwt_algorithm=(os.getenv("ADMIN_PANEL_JWT_ALGORITHM") or "HS256").strip(),
            admin_panel_jwt_signing_key=(os.getenv("ADMIN_PANEL_JWT_SIGNING_KEY") or "").strip() or None,
            admin_panel_jwt_public_key=(os.getenv("ADMIN_PANEL_JWT_PUBLIC_KEY") or "").strip() or None,
            admin_panel_jwt_audience=(os.getenv("ADMIN_PANEL_JWT_AUDIENCE") or "").strip() or None,
            enforce_token_module_match=_as_bool(os.getenv("ENFORCE_TOKEN_MODULE_MATCH"), True),
            license_extension_days=int(
                (os.getenv("ADMIN_PANEL_LICENSE_EXTENSION_DAYS") or os.getenv("LICENSE_EXTENSION_DAYS") or 0)
            ),
            cache_all_modules=_as_bool(os.getenv("CACHE_ALL_MODULES"), True),
        )

    def admin_url(self, path: str) -> str:
        path_clean = path if path.startswith("/") else f"/{path}"
        return f"{self.admin_panel_base_url}{path_clean}"

    def jwt_verification_key(self) -> str:
        algorithm = self.admin_panel_jwt_algorithm.upper()
        if algorithm.startswith("HS"):
            if not self.admin_panel_jwt_signing_key:
                raise RuntimeError("ADMIN_PANEL_JWT_SIGNING_KEY is required for HS algorithms")
            return self.admin_panel_jwt_signing_key
        if self.admin_panel_jwt_public_key:
            return self.admin_panel_jwt_public_key
        if self.admin_panel_jwt_signing_key:
            return self.admin_panel_jwt_signing_key
        raise RuntimeError(
            "ADMIN_PANEL_JWT_PUBLIC_KEY or ADMIN_PANEL_JWT_SIGNING_KEY is required for asymmetric JWT verification"
        )


settings = IntegrationSettings.from_env()
