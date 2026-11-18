"""Application-level encryption for stored credentials (plan §9.4).

Integration credentials are encrypted before they reach MongoDB, with a key that
lives outside the database (``DAYLINE_ENCRYPTION_KEY``, or a Docker secret file).
Several comma-separated keys rotate: the first encrypts, all of them decrypt.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CryptoError(Exception):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


class Crypto:
    def __init__(self, keys: str | None):
        parsed = [k.strip() for k in (keys or "").split(",") if k.strip()]
        self._fernet: MultiFernet | None = None
        if parsed:
            try:
                self._fernet = MultiFernet([Fernet(k.encode()) for k in parsed])
            except ValueError as e:
                raise CryptoError(
                    "DAYLINE_ENCRYPTION_KEY is not a valid Fernet key (use `dl integrations keygen`)"
                ) from e

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def _f(self) -> MultiFernet:
        if self._fernet is None:
            raise CryptoError("DAYLINE_ENCRYPTION_KEY is not set; cannot store or read credentials")
        return self._fernet

    def encrypt(self, data: dict[str, Any]) -> str:
        return self._f().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()

    def decrypt(self, token: str) -> dict[str, Any]:
        try:
            return json.loads(self._f().decrypt(token.encode()))
        except InvalidToken as e:
            raise CryptoError("credentials cannot be decrypted with the configured key(s)") from e

    def rotate(self, token: str) -> str:
        """Re-encrypt with the first (newest) key."""
        return self._f().rotate(token.encode()).decode()
