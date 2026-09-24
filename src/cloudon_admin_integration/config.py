import os
from dataclasses import dataclass


DEFAULT_CLIENT_BOOTSTRAP_PATH = "/api/client-auth/bootstrap/"
DEFAULT_CLIENT_TOKEN_PATH = "/api/client-auth/token/"
DEFAULT_EFFECTIVE_CONFIG_RESOLVE_PATH = "/api/client-auth/effective-configs/resolve/"
DEFAULT_EFFECTIVE_CONFIG_RECONCILE_PATH = "/api/client-auth/effective-configs/reconcile/"
DEFAULT_SYSTEM_LOG_INGEST_PATH = "/api/system-logs/ingest/"
DEFAULT_SYSTEM_LOG_INGEST_BULK_PATH = "/api/system-logs/ingest-bulk/"

# One namespace for everything the admin panel owns, on every Redis it writes to.
# A service is free to keep its own data in the same Redis under its own
# namespace: the panel only ever touches keys it finds in its own index.
DEFAULT_REDIS_KEY_PREFIX = "cloudon:admin_panel"
# Namespaces to read besides the one above. Empty, because the fleet shares one
# and there is nothing to migrate from: this exists only for the window when a
# namespace changes, where a service is restarted onto the new name before a
# resync has filled it. Set it then, and clear it when the old keys are gone.
DEFAULT_REDIS_KEY_PREFIX_FALLBACKS: tuple[str, ...] = ()


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


def _normalize_prefix(value: str | None) -> str | None:
    """Trim a Redis namespace to its bare form.

    Keys are composed as "<prefix>:<module>:<domain>:<company>", so a configured
    "admin_panel:" would otherwise produce a doubled separator.
    """
    if value is None:
        return None
    return value.strip().rstrip(":").strip() or None


def _fallback_prefixes(value: str | None, *, primary: str) -> tuple[str, ...]:
    """Namespaces to read from besides `primary`, in the order given.

    Unset means the namespace this package used before it had one name, so a
    service that never configured a prefix keeps serving from its existing keys
    until a resync has filled the new ones. An explicit empty value turns the
    fallback off, which is what ends a cutover.
    """
    raw = DEFAULT_REDIS_KEY_PREFIX_FALLBACKS if value is None else tuple(value.split(","))
    normalized = (_normalize_prefix(item) for item in raw)
    return _dedupe(tuple(item for item in normalized if item and item != primary))




@dataclass(frozen=True)
class IntegrationSettings:
    app_module_code: str
    app_module_codes: tuple[str, ...]
    admin_panel_base_url: str
    admin_panel_client_bootstrap_path: str
    admin_panel_client_token_path: str
    admin_panel_effective_config_resolve_path: str
    admin_panel_effective_config_reconcile_path: str
    admin_panel_system_log_ingest_path: str
    admin_panel_system_log_ingest_bulk_path: str
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
    enforce_token_module_match: bool
    license_extension_days: int
    integration_wrap_responses: bool
    integration_excluded_paths: tuple[str, ...]
    require_module_params: bool
    license_expiry_warning_days: int
    cache_stale_after_seconds: int
    # Defaulted so adding settings does not break callers that build this
    # explicitly (tests, and any consumer constructing it by hand).
    reconcile_interval_seconds: int = 300
    redis_key_prefix_fallbacks: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "IntegrationSettings":
        base_url = (os.getenv("DJANGO_API_URL") or "").strip().rstrip("/")
        raw_module_code = (os.getenv("APP_MODULE_CODE") or "").strip() or None
        # APP_MODULE_CODES is an allow-list, not a subscription: the panel pushes
        # every module a company holds into one cache, and a service reads what
        # its endpoints ask for. Left unset it restricts nothing, so a service
        # only declares it to deliberately refuse the rest.
        module_codes = _dedupe(_as_csv(os.getenv("APP_MODULE_CODES"), ()))
        if not module_codes:
            module_code = raw_module_code or "pharmacy_one"
        else:
            module_code = raw_module_code if raw_module_code in module_codes else module_codes[0]
        return cls(
            app_module_code=module_code or module_codes[0],
            app_module_codes=module_codes,
            admin_panel_base_url=base_url,
            admin_panel_client_bootstrap_path=(
                os.getenv("ADMIN_PANEL_CLIENT_BOOTSTRAP_PATH") or DEFAULT_CLIENT_BOOTSTRAP_PATH
            ).strip(),
            admin_panel_client_token_path=(
                os.getenv("ADMIN_PANEL_CLIENT_TOKEN_PATH") or DEFAULT_CLIENT_TOKEN_PATH
            ).strip(),
            admin_panel_effective_config_resolve_path=(
                os.getenv("ADMIN_PANEL_EFFECTIVE_CONFIG_RESOLVE_PATH") or DEFAULT_EFFECTIVE_CONFIG_RESOLVE_PATH
            ).strip(),
            admin_panel_effective_config_reconcile_path=(
                os.getenv("ADMIN_PANEL_EFFECTIVE_CONFIG_RECONCILE_PATH") or DEFAULT_EFFECTIVE_CONFIG_RECONCILE_PATH
            ).strip(),
            admin_panel_system_log_ingest_path=(
                os.getenv("ADMIN_PANEL_SYSTEM_LOG_INGEST_PATH") or DEFAULT_SYSTEM_LOG_INGEST_PATH
            ).strip(),
            admin_panel_system_log_ingest_bulk_path=(
                os.getenv("ADMIN_PANEL_SYSTEM_LOG_INGEST_BULK_PATH") or DEFAULT_SYSTEM_LOG_INGEST_BULK_PATH
            ).strip(),
            admin_panel_client_id=(os.getenv("ADMIN_PANEL_CLIENT_ID") or "").strip() or None,
            admin_panel_client_secret=(os.getenv("ADMIN_PANEL_CLIENT_SECRET") or "").strip() or None,
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS") or 10),
            sync_on_startup=_as_bool(os.getenv("SYNC_ON_STARTUP"), False),
            sync_key=(
                (os.getenv("ADMIN_PANEL_SYNC_KEY") or "").strip()
                or (os.getenv("SYNC_KEY") or "").strip()
                or None
            ),
            redis_host=(os.getenv("REDIS_HOST") or "localhost").strip(),
            redis_port=int(os.getenv("REDIS_PORT") or 6379),
            redis_db=int(os.getenv("REDIS_DB") or 0),
            redis_password=(os.getenv("REDIS_PASSWORD") or "").strip() or None,
            redis_key_prefix=_normalize_prefix(os.getenv("REDIS_KEY_PREFIX")) or DEFAULT_REDIS_KEY_PREFIX,
            redis_key_prefix_fallbacks=_fallback_prefixes(
                os.getenv("REDIS_KEY_PREFIX_FALLBACKS"),
                primary=_normalize_prefix(os.getenv("REDIS_KEY_PREFIX")) or DEFAULT_REDIS_KEY_PREFIX,
            ),
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
            # Background delta-sync cadence. 0 disables the loop, leaving the cache
            # to be refreshed by backend pushes and on-demand staleness refreshes.
            reconcile_interval_seconds=int(os.getenv("RECONCILE_INTERVAL_SECONDS") or 300),
        )

    def admin_url(self, path: str) -> str:
        path_clean = path if path.startswith("/") else f"/{path}"
        return f"{self.admin_panel_base_url}{path_clean}"


settings = IntegrationSettings.from_env()
