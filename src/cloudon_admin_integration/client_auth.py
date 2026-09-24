"""Recognising a caller from the credentials the panel cached here.

The panel used to sign a JWT and every service verified it with a public key.
That put a second secret in every `.env`, and made minting a token depend on the
panel being reachable from a machine whose whole point is to keep serving when it
is not.

The credential itself is cached instead — client id, the password hash the panel
stores, and the company it belongs to. A service checks a presented secret
against that hash locally and mints an opaque token of its own, held in Redis
under a digest. Nothing is signed, so nothing needs a key; and the token carries
the tenant scope, which is what keeps one client from reading another's data.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_TOKEN_TTL_SECONDS = 1800


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def verify_secret(raw_secret: str, encoded: str) -> bool:
    """Check a secret against Django's `make_password` output.

    Only PBKDF2-SHA256 is accepted, which is what the panel writes. An unknown
    algorithm is a refusal rather than a guess: a hash this cannot read is a
    hash this cannot check, and answering "yes" to one would be worse than
    failing to recognise the caller at all.
    """
    if not raw_secret or not encoded:
        return False
    parts = str(encoded).split("$")
    if len(parts) != 4:
        return False
    algorithm, iterations, salt, digest = parts
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        rounds = int(iterations)
    except (TypeError, ValueError):
        return False
    if rounds <= 0:
        return False
    computed = hashlib.pbkdf2_hmac("sha256", raw_secret.encode("utf-8"), salt.encode("utf-8"), rounds)
    return hmac.compare_digest(base64.b64encode(computed).decode("ascii"), digest)


def token_digest(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def mint_token() -> str:
    return secrets.token_urlsafe(48)


def session_for(client: dict[str, Any], *, ttl_seconds: int | None = None) -> tuple[str, dict[str, Any], int]:
    """A fresh token and the scope it stands for."""
    ttl = int(ttl_seconds or client.get("token_ttl_seconds") or DEFAULT_TOKEN_TTL_SECONDS)
    ttl = max(ttl, 1)
    token = mint_token()
    expires_at = _utc_now() + timedelta(seconds=ttl)
    session = {
        "token_type": "api_client",
        "client_id": client.get("client_id"),
        "company_id": client.get("company_id"),
        "company_code": client.get("company_code"),
        "company_name": client.get("company_name"),
        "infrastructure_id": client.get("infrastructure_id"),
        "infrastructure_domain": client.get("infrastructure_domain"),
        "infrastructure_serial_num": client.get("infrastructure_serial_num"),
        "expires_at": expires_at.isoformat(),
        "issued_at": _utc_now().isoformat(),
    }
    return token, session, ttl


def session_expired(session: dict[str, Any]) -> bool:
    raw = str(session.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= _utc_now()
