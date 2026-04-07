# cloudon-admin-integration

Reusable FastAPI integration for CloudOn Admin Panel:
- Public `/auth/token` bootstrap endpoint for external APIs
- Cached `/admin/parameters` bundle endpoint for app code and debugging
- Bearer JWT validation (`api_client` tokens)
- Redis-cached entitlement checks (module license/parameters)
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

`wire_integration(app)` also wires the public `/auth/token` route and global response/error formatting by default:

```json
{
  "success": true,
  "error": null,
  "message": null,
  "data": null
}
```

Errors use the same envelope shape with `message: null` and `data: null`.

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
- `APP_MODULE_CODES=pharmacy_one,rapid_test` is the preferred module allow-list. It can contain one value or many values.
- `APP_MODULE_CODE=pharmacy_one` remains a backward-compatible fallback for older single-module deployments.

## Protect endpoint

```python
from fastapi import Depends
from cloudon_admin_integration.dependencies import EntitlementContext, require_module_entitlement

@app.get("/hello")
async def hello(entitlement: EntitlementContext = Depends(require_module_entitlement)):
    return {
        "module": entitlement.module,
        "license": entitlement.license.model_dump(),
        "parameters": entitlement.parameters,
    }
```

For per-endpoint module checks:

```python
from cloudon_admin_integration.dependencies import require_module_entitlement_for

Depends(require_module_entitlement_for("pharmacy_one"))
```

For params-only injection:

```python
from fastapi import Depends
from cloudon_admin_integration import require_module_parameters, require_module_parameters_for

@app.get("/test")
async def test(
    current_params: dict = Depends(require_module_parameters),
    rapid_test_params: dict = Depends(require_module_parameters_for("rapid_test")),
):
    return {
        "current_params": current_params,
        "rapid_test_params": rapid_test_params,
    }
```

To inspect the full cached bundle for the logged-in client/company:

```python
from fastapi import Depends
from cloudon_admin_integration import require_all_module_entitlements
from cloudon_admin_integration.dependencies import EntitlementsContext

@app.get("/admin/parameters")
async def admin_parameters(ctx: EntitlementsContext = Depends(require_all_module_entitlements)):
    return ctx.model_dump()
```

To scope that same bundle to one or more modules:

```python
from cloudon_admin_integration import require_module_entitlements_for
from cloudon_admin_integration.config import settings

Depends(require_module_entitlements_for(settings.app_module_codes))
```

The singular helpers still return one `EntitlementContext`, but its public dump is intentionally small:

```json
{
  "module": "rapid_test",
  "license": {
    "expiration_date": "2026-08-21",
    "status": "active"
  },
  "parameters": {}
}
```

The plural helpers return an `EntitlementsContext` list-like root model, so `ctx.model_dump()` yields a plain list of the same module objects.

`require_module_entitlements(...)` stays branch-aware, while `require_all_module_entitlements` and `GET /admin/parameters` ignore any branch selector and return the full company bundle.

## Minimal external API setup

If you want the smallest possible integration surface in an external FastAPI app:

1. Keep the package install.
2. Add `wire_integration(app)`.
3. Define either `APP_MODULE_CODE` or `APP_MODULE_CODES`.
4. Use `require_module_entitlement` for a single protected module, or `require_module_entitlements` / `require_module_entitlements_for(...)` when you want the full bundle.
5. Call `POST /auth/token` on the external API to bootstrap and cache the client's full entitlement bundle.
6. Call `GET /admin/parameters` to inspect the full local cached bundle in the same compact shape.

The admin-panel URL and Redis are still runtime settings for the external API process, but they can live in shared deployment env/secrets rather than being hardcoded in the app itself. If you keep the default HS256 flow, the client secret doubles as the JWT verification key.

Webhook refreshes should target `POST /sync-redis-data` with the same `SYNC_KEY` that the backend sends in `X-Sync-Key`. The middleware now patches Redis directly from the webhook payload, so UI changes propagate without a full re-bootstrap. The legacy `/sync-single-license`, `/sync-single-param`, and `/sync-company-change` routes remain compatibility aliases and now apply the same incremental update logic.

`ADMIN_PANEL_CLIENT_ID` and `ADMIN_PANEL_CLIENT_SECRET` are only needed if you want `sync_on_startup=true` or you call `/auth/token` from a background job to prewarm the cache. They are not required for normal backend webhook refreshes.

## Runtime flow

1. Integration authenticates against the admin panel using `client_id` + `client_secret`.
2. The bootstrap response returns the client token plus the current entitlement bundle for all licensed modules.
3. Integration caches the bundle in local Redis as `entitlement:{domain}:{company_code}:{module_code}` and exposes a compact `{"module", "license", "parameters"}` view to app code.
4. Admin-panel signals keep the local cache fresh when licenses or module settings change.
5. API requests only read local Redis and validate the bearer token locally.
