"""Credentials of member tools, all derived from ``DAYLINE_TOOLS_SECRET``.

* Tool token (``dlk_...``): one per tool, deterministic, nothing stored. The tool's container
  gets it as ``DAYLINE_TOOL_TOKEN``; Dayline sends it as the bearer when calling the tool (so
  other containers on the `tools` network cannot), and the tool uses it to authenticate to
  the egress proxy.
* Grant (``dlt_...``): minted per tool call and passed in the call's ``_meta``. Names the
  tool, the user the call runs for and the scopes from the approved ``tool.yaml``; expires
  shortly after the call's timeout. The tool presents it to ``/internal/tool/*``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

GRANT_PREFIX = "dlt_"
TOOL_TOKEN_PREFIX = "dlk_"


class GrantError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _mac(secret: str, purpose: str, data: bytes) -> bytes:
    key = hmac.new(secret.encode(), purpose.encode(), hashlib.sha256).digest()
    return hmac.new(key, data, hashlib.sha256).digest()


def tool_token(secret: str, tool: str) -> str:
    return TOOL_TOKEN_PREFIX + _b64(_mac(secret, "tool-token", tool.encode()))


def check_tool_token(secret: str, tool: str, token: str) -> bool:
    return hmac.compare_digest(tool_token(secret, tool), token)


@dataclass(frozen=True)
class Grant:
    tool: str
    user_id: str
    scopes: frozenset[str]
    expires_at: float
    run_id: str | None = None


def mint_grant(
    secret: str,
    *,
    tool: str,
    user_id: str,
    scopes: list[str] | frozenset[str],
    ttl_s: float,
    run_id: str | None = None,
    now: float | None = None,
) -> str:
    payload = {
        "t": tool,
        "u": user_id,
        "s": sorted(scopes),
        "e": int((now if now is not None else time.time()) + ttl_s),
        "r": run_id,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    return f"{GRANT_PREFIX}{body}.{_b64(_mac(secret, 'grant', body.encode()))}"


def verify_grant(secret: str, token: str, *, now: float | None = None) -> Grant:
    if not token.startswith(GRANT_PREFIX) or token.count(".") != 1:
        raise GrantError("not a tool grant")
    body, _, sig = token[len(GRANT_PREFIX) :].partition(".")
    try:
        good = hmac.compare_digest(_unb64(sig), _mac(secret, "grant", body.encode()))
    except ValueError:
        good = False
    if not good:
        raise GrantError("bad signature")
    try:
        p = json.loads(_unb64(body))
        grant = Grant(p["t"], p["u"], frozenset(p["s"]), float(p["e"]), p.get("r"))
    except (ValueError, KeyError, TypeError) as e:
        raise GrantError("malformed grant") from e
    if grant.expires_at < (now if now is not None else time.time()):
        raise GrantError("grant expired")
    return grant
