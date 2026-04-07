# cloudon-admin-integration

Reusable FastAPI integration for CloudOn Admin Panel:
- Bearer JWT validation (`api_client` tokens)
- Redis-cached entitlement checks (company/module)
- Bootstrap + sync routes for license/params/company refresh
- Global API response envelope and exception normalization

## Install

```bash
pip install "git+https://github.com/cloudonorg/cloudon_admin_integration.git"
```

## Basic wiring

```python
from cloudon_admin_integration import wire_integration

wire_integration(app)
```

`wire_integration(app)` also wires global response/error formatting by default:

```json
{
  "success": true,
  "error": null,
  "message": null,
  "data": {}
}
```

Disable if needed:

```python
wire_integration(app, include_response_envelope=False)
```

Relevant env flags:
- `ADMIN_PANEL_CLIENT_ID` / `ADMIN_PANEL_CLIENT_SECRET` are used to bootstrap the local entitlement cache from the admin panel.
- By default, the returned API-client JWT is verified with `ADMIN_PANEL_CLIENT_SECRET`, so no separate JWT secret is required unless you override the signing strategy.
- `ADMIN_PANEL_CLIENT_BOOTSTRAP_PATH=/api/client-auth/bootstrap/`
- `REQUIRE_MODULE_PARAMS=true|false` (if true, empty params returns 403)
- `LICENSE_EXPIRY_WARNING_DAYS=10` (adds success `message` when license is close to expiration)
- `APP_MODULE_CODE=pharmacy_one` remains the primary module for backward-compatible single-module apps.
- `APP_MODULE_CODES=pharmacy_one,rapid_test` is optional and becomes the authoritative allow-list for multi-module token matching and bootstrap caching.

## Protect endpoint

```python
from fastapi import Depends
from cloudon_admin_integration.dependencies import EntitlementContext, require_module_entitlement

@app.get("/hello")
async def hello(entitlement: EntitlementContext = Depends(require_module_entitlement)):
    return {"company_id": entitlement.company_id, "params": entitlement.params}
```

For per-endpoint module checks:

```python
from cloudon_admin_integration.dependencies import require_module_entitlement_for

Depends(require_module_entitlement_for("pharmacy_one"))
```

To inspect every cached entitlement for the logged-in client/company:

```python
from fastapi import Depends
from cloudon_admin_integration import require_module_entitlements
from cloudon_admin_integration.dependencies import EntitlementsContext

@app.get("/entitlements")
async def entitlements(ctx: EntitlementsContext = Depends(require_module_entitlements)):
    return [item.model_dump() for item in ctx.entitlements]
```

To scope that same bundle to one or more modules:

```python
from cloudon_admin_integration import require_module_entitlements_for
from cloudon_admin_integration.config import settings

Depends(require_module_entitlements_for(settings.app_module_codes))
```

The singular helpers still return one `EntitlementContext`. The plural helpers return an `EntitlementsContext` wrapper with an `entitlements` list and accept no module filter, one module code, or multiple module codes.

## Minimal external API setup

If you want the smallest possible integration surface in an external FastAPI app:

1. Keep the package install.
2. Add `wire_integration(app)`.
3. Define either `APP_MODULE_CODE` or `APP_MODULE_CODES`.
4. Use `require_module_entitlement` for a single protected module, or `require_module_entitlements` / `require_module_entitlements_for(...)` when you want the full bundle.

The admin-panel URL, Redis, and bootstrap credentials are still runtime settings for the external API process, but they can live in shared deployment env/secrets rather than being hardcoded in the app itself. If you keep the default HS256 flow, the client secret doubles as the JWT verification key.

Webhook refreshes are simplest when they target `POST /sync-redis-data`. The legacy `/sync-single-license`, `/sync-single-param`, and `/sync-company-change` routes remain compatibility aliases and now perform a full cache refresh too.

## Runtime flow

1. Integration authenticates against the admin panel using `client_id` + `client_secret`.
2. The bootstrap response returns the client token plus the current entitlement bundle for all licensed modules.
3. Integration caches the bundle in local Redis as `entitlement:{domain}:{company_code}:{module_code}`.
4. Admin-panel signals keep the local cache fresh when licenses or module settings change.
5. API requests only read local Redis and validate the bearer token locally.
