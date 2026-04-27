# cloudon-admin-integration

Reusable FastAPI integration layer for CloudOn Admin Panel.

It provides:
- external API `/auth/token`
- local Redis cache of backend effective configs
- bearer token validation for `api_client` tokens
- dependency helpers for license and parameter checks
- webhook notification routes for refresh/reconcile
- consistent response envelope wiring

## Architecture

The admin backend is authoritative.

This package treats Redis as a rebuildable local cache of normalized effective configuration.

Runtime flow:
1. external API receives `client_id` + `client_secret`
2. package calls backend `POST /api/client-auth/bootstrap/`
3. backend returns token plus `effective_configs`
4. package stores those configs in Redis
5. protected API endpoints read only from Redis
6. backend notifications trigger refresh/reconcile, not direct state writes from webhook payloads

## Install

Add these dependencies to the external API:

```txt
git+https://github.com/cloudonorg/cloudon_admin_integration.git
cryptography
```

`cryptography` is required for `RS256` token verification.

### Private GitHub Access During Docker Builds

This package is installed from a private GitHub repository. Any external API image that installs this requirement during `docker compose build` must provide a GitHub token at build time.

Do not commit a real token to `README.md`, `requirements.txt`, `dockerfile`, `docker-compose.yml`, `.env`, or `.env.admin-panel`. Use a short-lived shell environment variable and pass it as a BuildKit secret.

Create a GitHub personal access token with read access to this repository:
- preferred: fine-grained token scoped to `cloudonorg/cloudon_admin_integration`
- required repository permission: `Contents: Read-only`
- fallback: classic token with `repo` scope, only if fine-grained tokens are blocked

Set the token only in the terminal that runs the build:

```bash
cd /path/to/external-api
read -rsp "GitHub token: " GITHUB_TOKEN; echo
export GITHUB_TOKEN

docker compose up -d --build

unset GITHUB_TOKEN
```

Use `docker compose`, not legacy `docker-compose`.

Example `dockerfile` pattern:

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.11

WORKDIR /app

COPY requirements.txt /app/
RUN --mount=type=secret,id=github_token,required=true \
    set -eu; \
    GITHUB_TOKEN="$(cat /run/secrets/github_token)"; \
    git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"; \
    pip install --no-cache-dir -r requirements.txt; \
    git config --global --unset-all url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf

COPY . /app
```

Example `docker-compose.yml` build secret:

```yaml
services:
  api:
    build:
      context: .
      dockerfile: dockerfile
      secrets:
        - github_token

secrets:
  github_token:
    environment: GITHUB_TOKEN
```

Recommended `.dockerignore` entries:

```text
.env
.env.*
.github-token
__pycache__/
*.pyc
```

If a token is accidentally pasted into chat, committed, logged, or shared, revoke it in GitHub and generate a new one.

## Minimal FastAPI Wiring

```python
from fastapi import Depends, FastAPI
from cloudon_admin_integration import (
    EntitlementContext,
    EntitlementsContext,
    require_module_entitlement,
    require_module_entitlements_for,
    require_module_parameters,
    require_module_parameters_for,
    wire_integration,
)
from cloudon_admin_integration.config import settings as integration_settings

app = FastAPI()
wire_integration(app)

@app.get("/test/app-module-code")
async def test_app_module_code(
    entitlement: EntitlementContext = Depends(require_module_entitlement),
    current_params: dict = Depends(require_module_parameters),
):
    return {
        "app_module_code": integration_settings.app_module_code,
        "module": entitlement.module,
        "license": entitlement.license.model_dump(),
        "current_parameters": current_params,
    }

@app.get("/test/app-module-codes")
async def test_app_module_codes(
    entitlements: EntitlementsContext = Depends(
        require_module_entitlements_for(integration_settings.app_module_codes)
    ),
):
    return entitlements.model_dump()

@app.get("/test/hard-coded")
async def test_hard_coded(
    pharmacy_one_params: dict = Depends(require_module_parameters_for("pharmacy_one")),
    update_items_params: dict = Depends(require_module_parameters_for("update_items")),
    open_cart_params: dict = Depends(require_module_parameters_for("open_cart")),
):
    return {
        "pharmacy_one": pharmacy_one_params,
        "update_items": update_items_params,
        "open_cart": open_cart_params,
    }
```

## What Gets Added To An External API

### 1. Requirements

```txt
git+https://github.com/cloudonorg/cloudon_admin_integration.git
cryptography
```

### 2. `.env.admin-panel`

Create one `.env.admin-panel` file in each external API.
It contains all variables needed by this integration: admin backend, token verification, module scope, and Redis cache.

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
- for a multi-module API, set `APP_MODULE_CODES`; `APP_MODULE_CODE` defaults to the first code if omitted
- `ADMIN_PANEL_JWT_AUDIENCE` can be omitted when the backend token has no audience
- with `RS256`, external APIs should only get the public key
- do not set `ADMIN_PANEL_JWT_SIGNING_KEY` on external APIs
- `localhost` inside the API container is the API container itself, not Redis
- with Docker Compose, use the Redis service name, usually `REDIS_HOST=redis`
- with manually started containers, use the Redis container name, for example `REDIS_HOST=pharmacyone_redis`, and put both containers on the same Docker network

These backend endpoint paths are fixed defaults in the package and usually should not be set per external API:
- `ADMIN_PANEL_CLIENT_BOOTSTRAP_PATH=/api/client-auth/bootstrap/`
- `ADMIN_PANEL_EFFECTIVE_CONFIG_RESOLVE_PATH=/api/client-auth/effective-configs/resolve/`
- `ADMIN_PANEL_EFFECTIVE_CONFIG_RECONCILE_PATH=/api/client-auth/effective-configs/reconcile/`

Only override them if the admin backend routes actually change.
`ADMIN_PANEL_CLIENT_TOKEN_PATH` is not used by this package.

Runtime behavior also has defaults:

```env
HTTP_TIMEOUT_SECONDS=10
INTEGRATION_WRAP_RESPONSES=true
CACHE_STALE_AFTER_SECONDS=3600
SYNC_ON_STARTUP=false
```

Optional service-level startup sync is only needed when the external API should bootstrap itself on startup:

```env
SYNC_ON_STARTUP=true
ADMIN_PANEL_CLIENT_ID="change_me"
ADMIN_PANEL_CLIENT_SECRET="change_me"
```

Optional webhook protection if sync routes are used:

```env
SYNC_KEY="change_me"
```

Actual cache keys look like:

```text
pharmacyone:integration:effective:{domain}:{company_code}:{module_code}:{branch_code_or_root}
```

### 3. Docker Compose

Typical external API compose additions:

```yaml
services:
  api:
    env_file:
      - .env.admin-panel
    depends_on:
      - redis

  redis:
    image: redis:7
```

## Endpoints Exposed By The Package

Public external-API endpoint:
- `POST /auth/token`

This endpoint:
- authenticates the client against backend
- fetches backend bootstrap payload
- stores `effective_configs` in Redis
- returns the backend token/bootstrap response

Webhook / sync endpoints:
- `POST /sync-redis-data`
- `POST /sync-single-license`
- `POST /sync-single-param`
- `POST /sync-company-change`
- `GET /get-redis-data`

Compatibility routes remain available, but the preferred model is:
- backend webhook as notification
- downstream refresh from backend authoritative endpoints

## Runtime Dependency Helpers

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

## Redis / Cache Behavior

Redis stores normalized effective-config rows including:
- effective config payload
- `version`
- `updated_at`
- `stale_at`

The cache is disposable.
If Redis is cleared, the external API can rebuild by calling `/auth/token` again for that client, or by running a refresh/reconcile path.

## RS256 Notes

Recommended production setup:
- backend signs client tokens with private key
- external APIs verify with public key

Backend env example:

```env
API_CLIENT_JWT_ALGORITHM="RS256"
API_CLIENT_JWT_SIGNING_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
API_CLIENT_JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"
API_CLIENT_JWT_AUDIENCE=""
```

External API `.env.admin-panel` example:

```env
ADMIN_PANEL_JWT_ALGORITHM=RS256
ADMIN_PANEL_JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"
# ADMIN_PANEL_JWT_AUDIENCE=
```

Important:
- do not use Redis session overrides for `RS256` verification
- `RS256` verification should use the configured public key

## Startup / Full Sync

Startup sync is disabled by default.

Service-level full bootstrap is only available if you set `SYNC_ON_STARTUP=true` and configure:
- `ADMIN_PANEL_CLIENT_ID`
- `ADMIN_PANEL_CLIENT_SECRET`

That is optional.
For the common per-client model, external APIs do not need service-wide credentials in env.

If you do enable startup bootstrap, the package uses:
- backend `/api/client-auth/bootstrap/`
- then writes returned `effective_configs` into Redis

## Troubleshooting

### `/auth/token` succeeds but Redis has only session keys

Cause:
- bootstrap path was overridden to the backend token endpoint instead of the backend bootstrap endpoint

Fix:
- remove the override, or set `ADMIN_PANEL_CLIENT_BOOTSTRAP_PATH="/api/client-auth/bootstrap/"`

### `Token invalid` with `RS256`

Check:
- `cryptography` is installed
- external API has `ADMIN_PANEL_JWT_PUBLIC_KEY`
- integration package version includes `RS256` verification fixes

### `License not found`

Check Redis for keys like:

```text
cloudon:integration:effective:{domain}:{company_code}:{module_code}:root
```

If missing, cache bootstrap/rebuild did not happen.

### `License not running`

Inspect the cached effective-config row.
If license metadata is active but `active/is_running` is false, backend effective-config generation is inconsistent and must be recomputed/fixed on backend.

### Startup fails connecting to Redis localhost

Cause:
- `REDIS_HOST` was not passed to the API container, so the package used its local default `localhost`
- in Docker, `localhost` points to the API container, not the Redis container

Fix:
- set `REDIS_HOST=redis` when Redis is a Docker Compose service named `redis`
- set `REDIS_HOST=pharmacyone_redis` if you are using that container name directly
- make sure the API and Redis containers are attached to the same Docker network

## Migration Notes

- backend remains authoritative
- Redis is cache only
- webhook payloads are notifications/triggers, not authoritative state
- old compatibility sync routes still exist
- new deployments should use `/bootstrap/`, `/resolve/`, `/reconcile/`, and local Redis reads
