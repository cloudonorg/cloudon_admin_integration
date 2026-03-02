# cloudon-admin-integration

Reusable FastAPI integration for CloudOn Admin Panel:
- Bearer JWT validation (`api_client` tokens)
- Redis-cached entitlement checks (company/module/branch)
- Sync routes for license/params/full rebuild
- Optional token proxy

## Install

```bash
pip install "git+https://github.com/cloudonorg/cloudon_admin_integration.git"
```

## Basic wiring

```python
from cloudon_admin_integration import wire_integration

wire_integration(app)
```

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
