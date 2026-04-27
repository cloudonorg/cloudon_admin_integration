# cloudon-admin-integration

Reusable FastAPI integration layer for CloudOn Admin Panel.

It provides:
- `POST /auth/token` for external API client authentication
- local Redis cache for backend effective configs
- bearer token validation for `api_client` tokens
- dependency helpers for license and parameter checks
- webhook sync routes for refresh/reconcile
- consistent response envelope wiring

## External API Setup

After this repository is public, a new external API only needs the package dependency, the admin-panel env file, Redis, and one FastAPI wiring call.

### 1. Requirements

Add the package to the external API `requirements.txt`:

```txt
git+https://github.com/cloudonorg/cloudon_admin_integration.git
cryptography
```

`cryptography` is required for `RS256` token verification.

No GitHub token is needed when this repository is public.

### 2. FastAPI Wiring

```python
from fastapi import FastAPI
from cloudon_admin_integration import wire_integration

app = FastAPI()
wire_integration(app)
```

That registers:
- `/auth/token`
- sync routes
- Redis startup/shutdown handling
- response envelope handling, unless disabled

### 3. `.env.admin-panel`

Create one `.env.admin-panel` file in each external API:

```env
# Admin backend base
DJANGO_API_URL="https://devadminpanel.cloudon.gr"

# JWT verification
ADMIN_PANEL_JWT_ALGORITHM="RS256"
ADMIN_PANEL_JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"
ADMIN_PANEL_JWT_AUDIENCE=""

# Module context
APP_MODULE_CODE="pharmacy_one"
APP_MODULE_CODES=pharmacy_one,rapid_test,convert,open_cart

# Redis integration cache
REDIS_HOST="redis"
REDIS_PORT="6379"
REDIS_DB="0"
REDIS_PASSWORD=""
REDIS_KEY_PREFIX="pharmacyone:integration"
```

Notes:
- for a single-module API, `APP_MODULE_CODE` is enough
- for a multi-module API, set `APP_MODULE_CODES`
- `APP_MODULE_CODE` defaults to the first value in `APP_MODULE_CODES` if omitted
- with `RS256`, external APIs should only receive the public key
- do not set `ADMIN_PANEL_JWT_SIGNING_KEY` on external APIs
- inside Docker, `localhost` is the API container itself; use `REDIS_HOST=redis` when the Redis service is named `redis`

### 4. Docker Compose

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

### 5. Build And Run

```bash
docker compose up -d --build
```

Use `docker compose`, not legacy `docker-compose`.

## Protected Endpoint Examples

Single module:

```python
from fastapi import Depends
from cloudon_admin_integration import EntitlementContext, require_module_entitlement, require_module_parameters

@app.get("/example")
async def example(
    entitlement: EntitlementContext = Depends(require_module_entitlement),
    params: dict = Depends(require_module_parameters),
):
    return {
        "module": entitlement.module,
        "license": entitlement.license.model_dump(),
        "parameters": params,
    }
```

Specific modules:

```python
from fastapi import Depends
from cloudon_admin_integration import require_module_parameters_for

@app.get("/pharmacy-one")
async def pharmacy_one(
    params: dict = Depends(require_module_parameters_for("pharmacy_one")),
):
    return params
```

Multi-module checks:

```python
from fastapi import Depends
from cloudon_admin_integration import EntitlementsContext, require_module_entitlements_for

@app.get("/modules")
async def modules(
    entitlements: EntitlementsContext = Depends(
        require_module_entitlements_for(["pharmacy_one", "open_cart"])
    ),
):
    return entitlements.model_dump()
```

## Runtime Flow

1. External API receives `client_id` and `client_secret` at `/auth/token`.
2. Integration calls backend `POST /api/client-auth/bootstrap/`.
3. Backend returns token plus `effective_configs`.
4. Integration stores normalized effective configs in Redis.
5. Protected endpoints validate the bearer token and read license/parameter state from Redis.
6. Backend notifications trigger refresh/reconcile; webhook payloads are treated as signals, not authoritative state.

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
SYNC_KEY="change_me"
```

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

## Runtime Helpers

Single-module checks:
- `require_module_entitlement`
- `require_module_entitlement_for(module_code)`
- `require_module_parameters`
- `require_module_parameters_for(module_code)`

Multi-module helpers:
- `require_module_entitlements`
- `require_module_entitlements_for(module_codes)`
- `require_all_module_entitlements`

Utility helpers:
- `validate_license(client_key, module_code, branch_code=None)`
- `get_parameters(client_key, module_code, branch_code=None)`
- `get_effective_config(client_key, module_code, branch_code=None)`

## Redis Cache

Redis stores normalized effective-config rows with:
- effective config payload
- `version`
- `updated_at`
- `stale_at`

The cache is disposable. If Redis is cleared, the external API can rebuild by calling `/auth/token` again for that client, or by running a refresh/reconcile path.

Actual cache keys look like:

```text
pharmacyone:integration:effective:{domain}:{company_code}:{module_code}:{branch_code_or_root}
```

## Troubleshooting

### Docker build cannot clone the package

If this happens, confirm `cloudonorg/cloudon_admin_integration` has been made public.

### Startup fails connecting to Redis localhost

Set `REDIS_HOST=redis` when Redis is a Docker Compose service named `redis`.

### `Token invalid` with `RS256`

Check:
- `cryptography` is installed
- external API has `ADMIN_PANEL_JWT_PUBLIC_KEY`
- external API does not use `ADMIN_PANEL_JWT_SIGNING_KEY`

### `License not found`

Check Redis for keys like:

```text
cloudon:integration:effective:{domain}:{company_code}:{module_code}:root
```

If missing, cache bootstrap/rebuild did not happen.

## Migration Notes

- backend remains authoritative
- Redis is cache only
- webhook payloads are notifications/triggers, not authoritative state
- old compatibility sync routes still exist
- new deployments should use `/bootstrap/`, `/resolve/`, `/reconcile/`, and local Redis reads
