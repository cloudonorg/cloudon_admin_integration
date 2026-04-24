import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    parts = tuple(item.strip() for item in value.split(",") if item.strip())
    return parts or default


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _normalize_key_material(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized.replace("\\n", "\n")


@dataclass(frozen=True)
class IntegrationSettings:
    app_module_code: str
    app_module_codes: tuple[str, ...]
    admin_panel_base_url: str
    admin_panel_client_bootstrap_path: str
    admin_panel_effective_config_resolve_path: str
    admin_panel_effective_config_reconcile_path: str
    admin_panel_client_id: str | None
    admin_panel_client_secret: str | None
    http_timeout_seconds: float
    sync_on_startup: bool
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
    integration_wrap_responses: bool
    integration_excluded_paths: tuple[str, ...]
    require_module_params: bool
    license_expiry_warning_days: int
    cache_stale_after_seconds: int

    @classmethod
    def from_env(cls) -> "IntegrationSettings":
        base_url = (os.getenv("DJANGO_API_URL") or "").strip().rstrip("/")
        raw_module_code = (os.getenv("APP_MODULE_CODE") or "").strip() or None
        module_codes = _dedupe(_as_csv(os.getenv("APP_MODULE_CODES"), ()))
        if not module_codes:
            module_code = raw_module_code or "pharmacy_one"
            module_codes = (module_code,)
        else:
            module_code = raw_module_code if raw_module_code in module_codes else module_codes[0]
        return cls(
            app_module_code=module_code or module_codes[0],
            app_module_codes=module_codes,
            admin_panel_base_url=base_url,
            admin_panel_client_bootstrap_path=(
                os.getenv("ADMIN_PANEL_CLIENT_BOOTSTRAP_PATH")
                or os.getenv("ADMIN_PANEL_CLIENT_TOKEN_PATH")
                or "/api/client-auth/bootstrap/"
            ).strip(),
            admin_panel_effective_config_resolve_path=(
                os.getenv("ADMIN_PANEL_EFFECTIVE_CONFIG_RESOLVE_PATH") or "/api/client-auth/effective-configs/resolve/"
            ).strip(),
            admin_panel_effective_config_reconcile_path=(
                os.getenv("ADMIN_PANEL_EFFECTIVE_CONFIG_RECONCILE_PATH") or "/api/client-auth/effective-configs/reconcile/"
            ).strip(),
            admin_panel_client_id=(os.getenv("ADMIN_PANEL_CLIENT_ID") or "").strip() or None,
            admin_panel_client_secret=(os.getenv("ADMIN_PANEL_CLIENT_SECRET") or "").strip() or None,
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS") or 10),
            sync_on_startup=_as_bool(os.getenv("SYNC_ON_STARTUP"), True),
            sync_key=(os.getenv("SYNC_KEY") or "").strip() or None,
            redis_host=(os.getenv("REDIS_HOST") or "localhost").strip(),
            redis_port=int(os.getenv("REDIS_PORT") or 6379),
            redis_db=int(os.getenv("REDIS_DB") or 0),
            redis_password=(os.getenv("REDIS_PASSWORD") or "").strip() or None,
            redis_key_prefix=(os.getenv("REDIS_KEY_PREFIX") or "pharmacyone:integration").strip(),
            admin_panel_jwt_algorithm=(os.getenv("ADMIN_PANEL_JWT_ALGORITHM") or "HS256").strip(),
            admin_panel_jwt_signing_key=_normalize_key_material(os.getenv("ADMIN_PANEL_JWT_SIGNING_KEY")),
            admin_panel_jwt_public_key=_normalize_key_material(os.getenv("ADMIN_PANEL_JWT_PUBLIC_KEY")),
            admin_panel_jwt_audience=(os.getenv("ADMIN_PANEL_JWT_AUDIENCE") or "").strip() or None,
            enforce_token_module_match=_as_bool(os.getenv("ENFORCE_TOKEN_MODULE_MATCH"), True),
            license_extension_days=int(
                (os.getenv("ADMIN_PANEL_LICENSE_EXTENSION_DAYS") or os.getenv("LICENSE_EXTENSION_DAYS") or 0)
            ),
            integration_wrap_responses=_as_bool(os.getenv("INTEGRATION_WRAP_RESPONSES"), True),
            integration_excluded_paths=_as_csv(
                os.getenv("INTEGRATION_EXCLUDED_PATHS"),
                ("/docs", "/redoc", "/openapi.json", "/favicon.ico"),
            ),
            require_module_params=_as_bool(os.getenv("REQUIRE_MODULE_PARAMS"), False),
            license_expiry_warning_days=int(os.getenv("LICENSE_EXPIRY_WARNING_DAYS") or 10),
            cache_stale_after_seconds=int(os.getenv("CACHE_STALE_AFTER_SECONDS") or 3600),
        )

    def admin_url(self, path: str) -> str:
        path_clean = path if path.startswith("/") else f"/{path}"
        return f"{self.admin_panel_base_url}{path_clean}"

    def jwt_verification_key(self) -> str:
        algorithm = self.admin_panel_jwt_algorithm.upper()
        if algorithm.startswith("HS"):
            if self.admin_panel_jwt_signing_key:
                return self.admin_panel_jwt_signing_key
            if self.admin_panel_client_secret:
                return self.admin_panel_client_secret
            raise RuntimeError("ADMIN_PANEL_CLIENT_SECRET is required for HS JWT verification")
        if self.admin_panel_jwt_public_key:
            return self.admin_panel_jwt_public_key
        if self.admin_panel_jwt_signing_key:
            return self.admin_panel_jwt_signing_key
        raise RuntimeError(
            "ADMIN_PANEL_JWT_PUBLIC_KEY or ADMIN_PANEL_JWT_SIGNING_KEY is required for asymmetric JWT verification"
        )


settings = IntegrationSettings.from_env()
