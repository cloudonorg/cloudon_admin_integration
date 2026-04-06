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
- `ADMIN_PANEL_CLIENT_BOOTSTRAP_PATH=/api/client-auth/bootstrap/`
- `REQUIRE_MODULE_PARAMS=true|false` (if true, empty params returns 403)
- `LICENSE_EXPIRY_WARNING_DAYS=10` (adds success `message` when license is close to expiration)

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

## Runtime flow

1. Integration authenticates against the admin panel using `client_id` + `client_secret`.
2. The bootstrap response returns the client token plus the current entitlement bundle for all licensed modules.
3. Integration caches the bundle in local Redis as `entitlement:{domain}:{company_code}:{module_code}`.
4. Admin-panel signals keep the local cache fresh when licenses or module settings change.
5. API requests only read local Redis and validate the bearer token locally.
