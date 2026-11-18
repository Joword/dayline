"""HTTP access to one integration target (plan §9.3).

``proxy``: ``none`` = direct only; a URL = always through that proxy; ``auto`` = try
direct first and fall back to the integration proxy (``DAYLINE_INTEGRATION_PROXY``,
else ``HTTPS_PROXY``) on connection failure, remembering what worked. Environment
proxies are never picked up implicitly (``trust_env=False``), so the setting means
what it says. ``ca_bundle`` adds a company CA for self-hosted services.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx

from app.integrations.models import (
    IntegrationAuthError,
    IntegrationError,
    IntegrationUnreachable,
    RateLimited,
)

TransportFactory = Callable[[str | None], httpx.AsyncBaseTransport]


class TargetClient:
    def __init__(
        self,
        proxy: str = "auto",
        *,
        fallback_proxy: str | None = None,
        ca_bundle: str | None = None,
        timeout: float = 20.0,
        transport_factory: TransportFactory | None = None,
    ):
        fallback = fallback_proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy == "none":
            self.routes: list[str | None] = [None]
        elif proxy == "auto":
            self.routes = [None, fallback] if fallback else [None]
        else:
            self.routes = [proxy]
        self.ca_bundle = ca_bundle
        self.timeout = timeout
        self.transport_factory = transport_factory
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self._start = 0  # index of the route that worked last time

    @property
    def route_used(self) -> str:
        r = self.routes[self._start]
        return "direct" if r is None else "proxy"

    def _client(self, route: str | None) -> httpx.AsyncClient:
        if route not in self._clients:
            kw: dict[str, Any] = {"timeout": self.timeout, "trust_env": False, "follow_redirects": True}
            if self.transport_factory is not None:
                kw["transport"] = self.transport_factory(route)
            else:
                if route:
                    kw["proxy"] = route
                kw["verify"] = self.ca_bundle or True
            self._clients[route] = httpx.AsyncClient(**kw)
        return self._clients[route]

    async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        order = self.routes[self._start :] + self.routes[: self._start]
        last: Exception | None = None
        for route in order:
            try:
                resp = await self._client(route).request(method, url, **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as e:
                last = e
                continue
            except httpx.HTTPError as e:
                raise IntegrationUnreachable(f"{type(e).__name__} contacting {httpx.URL(url).host}") from e
            self._start = self.routes.index(route)
            return resp
        raise IntegrationUnreachable(f"cannot reach {httpx.URL(url).host} ({type(last).__name__})") from last

    async def json(self, method: str, url: str, **kw: Any) -> Any:
        resp = await self.request(method, url, **kw)
        check_status(resp)
        return resp.json()

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()
        self._clients.clear()


def check_status(resp: httpx.Response) -> None:
    host = resp.request.url.host
    if resp.status_code in (401, 403):
        if resp.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimited(f"{host}: rate limited", _int(resp.headers.get("x-ratelimit-reset")))
        raise IntegrationAuthError(
            f"{host}: credentials rejected (HTTP {resp.status_code}); check the token / password"
        )
    if resp.status_code == 429:
        raise RateLimited(f"{host}: rate limited", _int(resp.headers.get("retry-after")))
    if resp.status_code >= 400:
        raise IntegrationError(f"{host}: HTTP {resp.status_code}")


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None
