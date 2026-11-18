"""API tokens: ``dl_<id>_<secret>``, shown once, stored as a SHA-256 hash.

The id part lets us find and revoke one token without storing the secret.
"""

from __future__ import annotations

import hashlib
import secrets

PREFIX = "dl_"


def new_token() -> tuple[str, str]:
    """Return (token_id, token)."""
    token_id = secrets.token_hex(4)
    return token_id, f"{PREFIX}{token_id}_{secrets.token_urlsafe(24)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_id_of(token: str) -> str | None:
    if not token.startswith(PREFIX):
        return None
    parts = token[len(PREFIX) :].split("_", 1)
    return parts[0] if len(parts) == 2 else None
