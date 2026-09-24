# cloudon-admin-integration

> **Upgrading**
>
> `wire_integration(app)` no longer publishes `POST /auth/token` or
> `GET /admin/parameters`. If your service needs them, pass
> `include_auth_routes=True` / `include_admin_routes=True`. The sync webhook
> routes are still registered by default.
>
> `GET /get-redis-data` now returns 400 unless you filter by `company_id`,
> `company_code`, `module_code` or `domain`, or pass `all_companies=true`.
> Unscoped, it returned every tenant the middleware had cached.
>
> Entitlement reads are served from Redis. The admin panel is consulted only when
> a record is missing or stale, so `RECONCILE_INTERVAL_SECONDS` (default 300)
> now drives a background delta sync instead of a call per request. Set it to 0
> to disable the loop.

Reusable FastAPI integration layer for CloudOn Admin Panel.

It provides:
- `POST /auth/token` for external API client authentication, when asked for
  (`include_auth_routes=True`)
- local Redis cache for backend effective configs
- bearer token validation for `api_client` tokens
- header-scoped helpers for partner endpoints that use their own auth
- dependency helpers for license and parameter checks
- webhook sync routes that apply backend payloads directly to Redis
- consistent response envelope wiring

## External API Setup

A new external API only needs the package dependency, the admin-panel env file, Redis, and one FastAPI wiring call.

### 1. Requirements

Add these lines to the external API `requirements.txt`:

```txt
git+https://github.com/cloudonorg/cloudon_admin_integration.git
```

Pin it to a commit — a bare URL installs whatever the default branch holds on
build day, so two rebuilds of the same tag can ship different integration code.

### 2. `.env.admin-panel`

Create one `.env.admin-panel` file in each external API. Five values are all a
service needs to read licences and parameters:

```env
# Where the panel is. Change it here if the panel's domain moves.
DJANGO_API_URL="https://devadminpanel.cloudon.gr"

# Must match the panel's sync key exactly. It authenticates the pushes that keep
# this cache current, and the bootstrap that refills it after a stop or rebuild.
ADMIN_PANEL_SYNC_KEY="change_me"

# Redis holding the panel's data on this server.
REDIS_HOST="redis"
REDIS_PORT="6379"
REDIS_DB="0"
REDIS_PASSWORD=""
```

Everything below is optional, and most services set none of it.

```env
# The module a helper assumes when an endpoint does not name one. Declare it
# once, beside the app; endpoints that want another module name it themselves.
APP_MODULE_CODE="pharmacy_one"

# An allow-list, not a subscription. One cache holds every module a company has,
# and unset restricts nothing — set this only to deliberately refuse the rest.
APP_MODULE_CODES=pharmacy_one,rapid_test

# The panel owns this namespace and every service shares it, so neither of these
# is normally set: the defaults are "cloudon:admin_panel" and no fallback. They
# exist for the window when a namespace changes — a service is restarted onto the
# new name before a resync has filled it, so it reads the old one on a miss until
# the old keys are deleted.
REDIS_KEY_PREFIX="cloudon:admin_panel"
REDIS_KEY_PREFIX_FALLBACKS="the-old-namespace"
```

### Choosing how a caller is identified

The package offers two, and the choice decides what else the service must do.

**Bearer token** (`require_module_entitlement` and friends) — the client posts
its `client_id` and `client_secret` to this service's `/auth/token`
(`wire_integration(app, include_auth_routes=True)`). The secret is checked
against the credential the panel cached here, and the service mints an opaque
token of its own, held in Redis under a digest of it until it expires.

The token *is* the tenant scope: it resolves to one company, so a caller cannot
ask about another, and the endpoint needs no auth of its own. Nothing is signed,
so there is no key to distribute or rotate, and a client can still authenticate
while the panel is unreachable — which is the point of holding its data here. A
credential the cache has not seen yet is checked with the panel once, and cached
on the way through.

**Headers** (`require_header_module_entitlement_for`) — the caller names the
company in `X-Company-Code` and `X-Domain`. Nothing is verified: the scope is
whatever the caller says. **The endpoint must authenticate the caller itself**,
as the transactions API does with HTTP Basic before it reads an entitlement.
Without that, anyone who can reach the service can read any client's parameters,
IDIKA credentials included.

Module notes:
- `APP_MODULE_CODE` is the default module used by helpers without an explicit module code.
- For a multi-module API, either name the module per endpoint or set `APP_MODULE_CODES`;
  `APP_MODULE_CODE` defaults to the first value when it is not in the list.
- The panel pushes every module a company holds, so the cache already has them
  whether or not this service lists them.

Security notes:
- inside Docker, `localhost` is the API container itself; use `REDIS_HOST=redis` when the Redis service is named `redis`
- `ADMIN_PANEL_SYNC_KEY` must match the admin backend `SYNC_KEY` exactly so webhook cache refresh requests are accepted
- `SYNC_KEY` is still supported as a fallback for older integrations, but new APIs should use `ADMIN_PANEL_SYNC_KEY` in `.env.admin-panel`

### 3. Docker Compose

Typical external API compose setup:

```yaml
services:
  api:
    build:
      context: .
      dockerfile: dockerfile
    env_file:
      - .env
      - .env.admin-panel
    depends_on:
      - redis

  redis:
    image: redis:7
    env_file:
      - .env.admin-panel
```

Recommended `.dockerignore`:

```text
.env
.env.*
__pycache__/
*.pyc
```

### 4. Build And Run

```bash
docker compose up -d --build
```

Use `docker compose`, not legacy `docker-compose`.

## What To Add In `app.py`

If the API runs through Docker Compose with `env_file`, the environment is already loaded before Python starts.
For local development with `python-dotenv`, load `.env.admin-panel` before importing `cloudon_admin_integration`, because integration settings are read at import time.

Full import block:

```python
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.admin-panel")

from fastapi import Depends, FastAPI, Request
from cloudon_admin_integration import (
    EntitlementContext,
    EntitlementsContext,
    require_module_entitlement,
    require_module_entitlement_for,
    require_module_entitlements,
    require_module_entitlements_for,
    require_all_module_entitlements,
    require_module_parameters,
    require_module_parameters_for,
    require_header_module_entitlement_for,
    require_header_module_parameters_for,
    wire_integration,
)
from cloudon_admin_integration.config import settings as integration_settings

app = FastAPI()
wire_integration(app)
```

`wire_integration(app)` registers:
- sync routes (`/sync-redis-data` and friends)
- Redis startup/shutdown handling
- response envelope handling

If an API already has its own response wrapper, you can disable only the envelope:

```python
wire_integration(app, include_response_envelope=False)
```

## Common Endpoint Patterns

### Default Module License

Use `require_module_entitlement` when the endpoint protects the default `APP_MODULE_CODE`.

```python
@app.get("/default-module/status")
async def default_module_status(
    entitlement: EntitlementContext = Depends(require_module_entitlement),
):
    return {
        "module": entitlement.module,
        "license": entitlement.license.model_dump(),
        "company_code": entitlement.company_code,
        "domain": entitlement.infrastructure_domain,
        "effective_config": entitlement.effective_config,
    }
```

### Specific Module License

Use `require_module_entitlement_for("module_code")` when one route belongs to a specific module.
This validates the bearer token, license, company, domain, and optional branch scope.

```python
@app.get("/retail-zoom/status")
async def retail_zoom_status(
    entitlement: EntitlementContext = Depends(require_module_entitlement_for("retail_zoom")),
):
    return {
        "module": entitlement.module,
        "license": entitlement.license.model_dump(),
        "parameters": entitlement.parameters,
        "domain": entitlement.infrastructure_domain,
        "company_code": entitlement.company_code,
    }
```

### Default Module Parameters

Use `require_module_parameters` when the endpoint only needs parameters for `APP_MODULE_CODE`.

```python
@app.get("/default-module/parameters")
async def default_module_parameters(
    params: dict = Depends(require_module_parameters),
):
    return params
```

### Specific Module Parameters

Use `require_module_parameters_for("module_code")` when the endpoint needs parameters for a specific module.

```python
@app.get("/sinopsis/parameters")
async def sinopsis_parameters(
    params: dict = Depends(require_module_parameters_for("sinopsis")),
):
    return params
```

When no branch is present in the token or `X-Branch-Code`, parameter helpers return the full module parameter payload for that client. Company-only modules return the flat parameter dict. Branch-based modules return:

```json
{
  "mode": "BRANCHES",
  "master": {},
  "branches": [
    {
      "branch_id": "branch-uuid",
      "branch_code": 101,
      "branch_name": "Main Branch",
      "live": true
    }
  ]
}
```

When a branch is present in the token or `X-Branch-Code`, parameter helpers return only that branch's parameter dict.

### Partner Or Header-Scoped Endpoints

Use `require_header_module_entitlement_for("module_code")` or `require_header_module_parameters_for("module_code")`
when the route authenticates the caller itself, for example with Basic Auth, and only wants Admin Panel
Redis to answer whether the requested client has a running license and parameters.

These helpers do not require a bearer token. They read the target from headers:

- `X-Domain` or `domain`
- `X-Company-Code` or `company`
- `X-Branch-Code` or `branch`, optional

```python
from fastapi import Depends
from fastapi.security import HTTPBasicCredentials
from cloudon_admin_integration import (
    EntitlementContext,
    require_header_module_entitlement_for,
)

@app.get("/retail_zoom/day_transactions")
async def retail_zoom_day_transactions(
    credentials: HTTPBasicCredentials = Depends(partner_basic_auth),
    entitlement: EntitlementContext = Depends(require_header_module_entitlement_for("retail_zoom")),
):
    return {
        "company_code": entitlement.company_code,
        "branch_code": entitlement.branch_code,
        "parameters": entitlement.parameters,
    }
```

The route remains responsible for authenticating the partner. The helper treats headers as the requested
target and allows the request only when the local Redis entitlement for that module/company is active.

### Configured Module List

Use `require_module_entitlements` to load entitlements for the modules configured in `APP_MODULE_CODES`.

```python
@app.get("/configured-modules")
async def configured_modules(
    entitlements: EntitlementsContext = Depends(require_module_entitlements),
):
    return entitlements.model_dump()
```

### Explicit Module List

Use `require_module_entitlements_for([...])` to load a known subset of modules.

```python
@app.get("/selected-modules")
async def selected_modules(
    entitlements: EntitlementsContext = Depends(
        require_module_entitlements_for(["pharmacy_one", "open_cart"])
    ),
):
    return entitlements.model_dump()
```

A single module string also works:

```python
@app.get("/retail-zoom/entitlements")
async def retail_zoom_entitlements(
    entitlements: EntitlementsContext = Depends(require_module_entitlements_for("retail_zoom")),
):
    return entitlements.model_dump()
```

### All Company-Level Entitlements

Use `require_all_module_entitlements` when you need all company-level module entitlements currently known in Redis.

```python
@app.get("/all-modules")
async def all_modules(
    entitlements: EntitlementsContext = Depends(require_all_module_entitlements),
):
    return entitlements.model_dump()
```

## Reading Entitlement Data

`EntitlementContext` is returned by single-module license helpers. Useful fields:

```python
@app.get("/example")
async def example(
    entitlement: EntitlementContext = Depends(require_module_entitlement_for("retail_zoom")),
):
    effective_config = entitlement.effective_config or {}
    domain = effective_config.get("infrastructure_domain") or entitlement.infrastructure_domain
    company = effective_config.get("company_code") or entitlement.company_code

    return {
        "module": entitlement.module,
        "module_code": entitlement.module_code,
        "license": entitlement.license.model_dump(),
        "parameters": entitlement.parameters,
        "effective_config": entitlement.effective_config,
        "domain": domain,
        "company": company,
        "branch_code": entitlement.branch_code,
    }
```

`EntitlementsContext` is returned by multi-module helpers. It can be dumped directly or iterated:

```python
@app.get("/modules/simple")
async def modules_simple(
    entitlements: EntitlementsContext = Depends(require_module_entitlements_for(["sinopsis", "retail_zoom"])),
):
    return [
        {
            "module": item.module,
            "license": item.license.model_dump(),
            "parameters": item.parameters,
        }
        for item in entitlements
    ]
```

`integration_settings` exposes the loaded integration env:

```python
@app.get("/integration/settings")
async def integration_settings_view():
    return {
        "default_module": integration_settings.app_module_code,
        "modules": integration_settings.app_module_codes,
        "redis_prefix": integration_settings.redis_key_prefix,
    }
```

## Auth And Cache Flow

1. The external API calls `POST /auth/token` with `client_id` and `client_secret`.
2. Integration calls backend `POST /api/client-auth/bootstrap/`.
3. Backend returns a bearer token plus `effective_configs`.
4. Integration stores normalized effective configs in Redis.
5. Protected endpoints validate the bearer token and read license/parameter state from Redis.
6. Backend sync events apply the effective-config payload directly to Redis.

Example token request:

```bash
curl -X POST https://external-api.example.com/auth/token \
  -H "Content-Type: application/json" \
  -d '{"client_id":"CLIENT_ID","client_secret":"CLIENT_SECRET"}'
```

Use the returned token on protected routes:

```bash
curl https://external-api.example.com/retail-zoom/status \
  -H "Authorization: Bearer API_CLIENT_TOKEN"
```

Optional scope headers:
- `X-Company-Id`
- `X-Company-Code`
- `X-Infrastructure-Domain` or `X-Domain`
- `X-Branch-Code`
- Partner/header-scoped helpers also accept lowercase legacy headers `domain`, `company`, and `branch`.

## Exposed Endpoints

Public external-API endpoint:
- `POST /auth/token`

Sync endpoints:
- `POST /sync-redis-data`
- `POST /sync-single-license`
- `POST /sync-single-param`
- `POST /sync-company-change`
- `GET /get-redis-data`

If sync routes are used, set a shared webhook secret:

```env
ADMIN_PANEL_SYNC_KEY="change_me"
```

It must match the backend admin-panel `SYNC_KEY` exactly. If the external API does not define it, `/sync-redis-data`
will reject or fail webhook cache refresh requests. Older projects may still use `SYNC_KEY`, but `.env.admin-panel`
with `ADMIN_PANEL_SYNC_KEY` is the preferred setup.

## Optional Startup Sync

Startup sync is disabled by default.

Enable it only when the external API should bootstrap itself on startup:

```env
SYNC_ON_STARTUP=true
ADMIN_PANEL_CLIENT_ID="change_me"
ADMIN_PANEL_CLIENT_SECRET="change_me"
```

For the common per-client model, external APIs do not need service-wide client credentials in env.

## Defaults Usually Left Alone

These backend paths are fixed defaults and normally should not be configured per external API:

```env
ADMIN_PANEL_CLIENT_BOOTSTRAP_PATH=/api/client-auth/bootstrap/
ADMIN_PANEL_EFFECTIVE_CONFIG_RESOLVE_PATH=/api/client-auth/effective-configs/resolve/
ADMIN_PANEL_EFFECTIVE_CONFIG_RECONCILE_PATH=/api/client-auth/effective-configs/reconcile/
```

Runtime defaults:

```env
HTTP_TIMEOUT_SECONDS=10
INTEGRATION_WRAP_RESPONSES=true
CACHE_STALE_AFTER_SECONDS=3600
SYNC_ON_STARTUP=false
```

## Runtime Helpers Reference

Single-module checks:
- `require_module_entitlement`: validates and returns `EntitlementContext` for `APP_MODULE_CODE`
- `require_module_entitlement_for(module_code)`: validates and returns `EntitlementContext` for an explicit module
- `require_module_parameters`: validates and returns parameters for `APP_MODULE_CODE`
- `require_module_parameters_for(module_code)`: validates and returns parameters for an explicit module
- `require_header_module_entitlement_for(module_code)`: validates cached entitlement from headers, without bearer-token auth
- `require_header_module_parameters_for(module_code)`: returns cached parameters from headers, without bearer-token auth

Multi-module helpers:
- `require_module_entitlements`: returns entitlements for `APP_MODULE_CODES`
- `require_module_entitlements_for(module_codes)`: returns entitlements for one module or a list of modules
- `require_all_module_entitlements`: returns all company-level entitlements known in Redis

Utility helpers:
- `validate_license(client_key, module_code, branch_code=None)`
- `get_parameters(client_key, module_code, branch_code=None)`
- `get_effective_config(client_key, module_code, branch_code=None)`

Lower-level helpers:
- `get_cache()` returns the integration Redis cache client
- `get_settings()` returns loaded integration settings
- `require_valid_api_client_token` validates the bearer token and returns token claims

## Redis Cache

Redis stores normalized effective-config rows with:
- effective config payload
- `version`
- `updated_at`
- `stale_at`

The cache is disposable. If Redis is cleared, the external API can rebuild by calling `/auth/token` again for that client, or by running a refresh/reconcile path.

Actual cache keys look like:

```text
cloudon:admin_panel:{module_code}:{domain}:{company_code}
```

The value is one JSON document for the company-level module entitlement. License state lives at the
company/module level. Parameters can be flat company parameters or a branch container:

```json
{
  "module_code": "retail_zoom",
  "domain": "pharmacydemo",
  "company_code": 10,
  "application_id": "8d2f...",
  "application_status": "RUNNING",
  "application_expires_at": "2026-12-31",
  "is_running": true,
  "license_to_date": "2026-12-31",
  "params": {
    "mode": "BRANCHES",
    "master": {},
    "branches": [
      {
        "branch_code": 101,
        "branch_name": "Main",
        "retail_zoom_enabled": true
      }
    ]
  }
}
```

## Troubleshooting

### Docker build cannot clone the package

If this happens, confirm `cloudonorg/cloudon_admin_integration` is public and the external API is using:

```txt
git+https://github.com/cloudonorg/cloudon_admin_integration.git
```

### Startup fails connecting to Redis localhost

Set `REDIS_HOST=redis` when Redis is a Docker Compose service named `redis`.

### `Token invalid`

A token means nothing outside the service that minted it, and its entry expires.
Check:
- the token came from *this* service's `/auth/token`, not another one's
- it has not expired: `token_ttl_seconds` on the client, 30 minutes by default
- the credential still exists in the cache and is active — withdrawing one drops
  the sessions it minted

### `License not found`

Check:
- `/auth/token` was called successfully for that client
- `APP_MODULE_CODES` includes the module you are trying to read
- backend bootstrap returned an `effective_config` for that module/company/domain/branch
- Redis contains keys like:

```text
cloudon:admin_panel:{module_code}:{domain}:{company_code}
```

### Parameters are empty

Check:
- the license exists and is active
- the backend effective config contains `parameters`
- `X-Branch-Code` is only sent when you want one branch's parameters
- without `X-Branch-Code`, branch-based modules return the full `{mode, master, branches}` payload

## Migration Notes

- backend remains authoritative
- Redis is cache only
- webhook payloads are applied directly when they include an effective config payload
- old compatibility sync routes still exist
- new deployments should use `/bootstrap/`, `/resolve/`, `/reconcile/`, and local Redis reads
