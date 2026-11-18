"""Who is calling.

* Personal token (``dl_...``, created with ``dl token create`` or ``POST /me/tokens``)
  -> that user.
* Service token (``DAYLINE_API_TOKEN``, for trusted integrations such as the DingTalk
  bot or a web BFF) -> acts for the user named in ``X-Dayline-User``; without that
  header it can only call endpoints that need no identity.
* No token: allowed only when ``DAYLINE_ENV=dev`` and no service token is configured
  (local development); the user then comes from ``X-Dayline-User`` or ``DAYLINE_USER``.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request

from app.core.security import hash_token, token_id_of


@dataclass(frozen=True)
class Caller:
    user_id: str | None
    client_id: str
    via: Literal["token", "service", "dev"]


def _unauthorized(msg: str = "unauthorized") -> HTTPException:
    return HTTPException(401, msg, headers={"WWW-Authenticate": "Bearer"})


async def caller(request: Request) -> Caller:
    s = request.app.state.services
    settings = s.settings
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    if token:
        if settings.api_token is not None and hmac.compare_digest(
            token, settings.api_token.get_secret_value()
        ):
            return Caller(
                request.headers.get("x-dayline-user") or None,
                request.headers.get("x-dayline-client") or "service",
                "service",
            )
        if s.users is not None and token_id_of(token):
            user = await s.users.find_by_token_hash(hash_token(token))
            if user is not None:
                await s.users.touch_token(user.id, token_id_of(token))
                return Caller(user.id, "dayline", "token")
        raise _unauthorized()
    if settings.env == "dev" and settings.api_token is None:
        return Caller(request.headers.get("x-dayline-user") or settings.user, "dayline", "dev")
    raise _unauthorized()


async def user_caller(request: Request) -> Caller:
    c = await caller(request)
    if not c.user_id:
        raise HTTPException(400, "this endpoint acts for a user: send X-Dayline-User with the service token")
    return c
