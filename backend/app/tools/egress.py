"""Egress proxy for member tools (plan §8.4): outbound access only to declared hosts.

Tool containers sit on the internal `tools` network; their only way out is this proxy
(``HTTPS_PROXY`` / ``HTTP_PROXY`` set by ``dl tools compose``). Each tool authenticates with
``Proxy-Authorization: Basic <tool>:<tool token>`` and may reach the hosts in its
``network_egress``:

  jira.company.com         ports 80 and 443
  jira.company.com:8443    that port only
  *.company.com            any subdomain (not company.com itself), ports 80 and 443

HTTPS goes through ``CONNECT`` (the proxy never sees the content); plain HTTP is forwarded
with ``Connection: close``. An optional upstream proxy (intranet profile) is used for both.
Rules come from ``DAYLINE_EGRESS_RULES`` (JSON ``{tool: [hosts]}``), written by ``dl tools compose``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from urllib.parse import urlsplit

from app.tools.tokens import check_tool_token

log = logging.getLogger("dayline.tools.egress")

MAX_HEAD = 16_384
DEFAULT_PORTS = (80, 443)
_HOP = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}


def _split_hostport(s: str, default: int | None = None) -> tuple[str, int | None]:
    s = s.strip().lower()
    if s.startswith("["):  # [v6]:port
        host, _, rest = s[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif s.count(":") == 1:
        host, _, port = s.partition(":")
    else:
        host, port = s, ""
    return host.rstrip("."), int(port) if port.isdigit() else default


def host_allowed(rules: list[str], host: str, port: int) -> bool:
    host = host.lower().rstrip(".")
    for rule in rules:
        r_host, r_port = _split_hostport(rule)
        if (port not in DEFAULT_PORTS) if r_port is None else (port != r_port):
            continue
        if r_host.startswith("*."):
            if host.endswith(r_host[1:]) and host != r_host[2:]:
                return True
        elif host == r_host:
            return True
    return False


class EgressProxy:
    def __init__(
        self,
        rules: dict[str, list[str]],
        secret: str,
        *,
        upstream: str | None = None,
        connect_timeout: float = 10.0,
        idle_timeout: float = 300.0,
    ):
        self.rules = rules
        self.secret = secret
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.upstream = None
        self.upstream_auth = None
        if upstream:
            u = urlsplit(upstream)
            self.upstream = (u.hostname or "", u.port or 3128)
            if u.username:
                cred = f"{u.username}:{u.password or ''}".encode()
                self.upstream_auth = "Basic " + base64.b64encode(cred).decode()

    async def serve(self, host: str = "0.0.0.0", port: int = 3128) -> asyncio.Server:
        return await asyncio.start_server(self._handle, host, port, limit=MAX_HEAD * 4)

    # --- request handling ---------------------------------------------------------------

    def _tool(self, headers: dict[str, str]) -> str | None:
        auth = headers.get("proxy-authorization", "")
        if not auth.lower().startswith("basic "):
            return None
        try:
            user, _, password = base64.b64decode(auth[6:].strip()).decode().partition(":")
        except ValueError:
            return None
        if user in self.rules and check_tool_token(self.secret, user, password):
            return user
        return None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.connect_timeout)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
            await _close(writer)
            return
        if len(head) > MAX_HEAD:
            await _reply(writer, 431, "Request Header Fields Too Large")
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            await _reply(writer, 400, "Bad Request")
            return
        headers: dict[str, str] = {}
        raw_headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
                raw_headers.append((k.strip(), v.strip()))
        tool = self._tool(headers)
        if tool is None:
            await _reply(
                writer,
                407,
                "Proxy Authentication Required",
                {"Proxy-Authenticate": 'Basic realm="dayline-tools"'},
            )
            return
        if method.upper() == "CONNECT":
            host, port = _split_hostport(target)
            if not host or port is None:
                await _reply(writer, 400, "Bad Request")
                return
            await self._connect(reader, writer, tool, host, port)
            return
        u = urlsplit(target)
        if u.scheme != "http" or not u.hostname:
            await _reply(writer, 400, "Bad Request", body="use CONNECT for https; absolute http URLs only")
            return
        await self._forward(reader, writer, tool, method, u, version, raw_headers)

    def _decide(self, tool: str, host: str, port: int) -> bool:
        ok = host_allowed(self.rules.get(tool, []), host, port)
        log.info("egress", extra={"tool": tool, "host": host, "port": port, "allowed": ok})
        return ok

    async def _open(self, host: str, port: int):
        return await asyncio.wait_for(asyncio.open_connection(host, port), self.connect_timeout)

    async def _connect(self, reader, writer, tool: str, host: str, port: int) -> None:
        if not self._decide(tool, host, port):
            await _reply(writer, 403, "Forbidden", body=f"{host}:{port} is not in network_egress of {tool}")
            return
        try:
            if self.upstream:
                ur, uw = await self._open(*self.upstream)
                req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                if self.upstream_auth:
                    req += f"Proxy-Authorization: {self.upstream_auth}\r\n"
                uw.write((req + "\r\n").encode())
                await uw.drain()
                resp = await asyncio.wait_for(ur.readuntil(b"\r\n\r\n"), self.connect_timeout)
                if b" 200" not in resp.split(b"\r\n", 1)[0]:
                    await _close(uw)
                    await _reply(writer, 502, "Bad Gateway", body="upstream proxy refused the tunnel")
                    return
            else:
                ur, uw = await self._open(host, port)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await _reply(writer, 502, "Bad Gateway")
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._splice(reader, writer, ur, uw)

    async def _forward(self, reader, writer, tool, method, u, version, raw_headers) -> None:
        port = u.port or 80
        if not self._decide(tool, u.hostname, port):
            await _reply(
                writer, 403, "Forbidden", body=f"{u.hostname}:{port} is not in network_egress of {tool}"
            )
            return
        kept = [(k, v) for k, v in raw_headers if k.lower() not in _HOP]
        if self.upstream:
            line = f"{method} {u.geturl()} {version}"
            if self.upstream_auth:
                kept.append(("Proxy-Authorization", self.upstream_auth))
        else:
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            line = f"{method} {path} {version}"
        head = line + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in kept) + "Connection: close\r\n\r\n"
        try:
            ur, uw = await self._open(*(self.upstream or (u.hostname, port)))
        except (OSError, TimeoutError):
            await _reply(writer, 502, "Bad Gateway")
            return
        uw.write(head.encode("latin-1"))
        await uw.drain()
        await self._splice(reader, writer, ur, uw)

    async def _splice(self, cr, cw, ur, uw) -> None:
        async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                while data := await asyncio.wait_for(r.read(65536), self.idle_timeout):
                    w.write(data)
                    await w.drain()
            except (OSError, TimeoutError, asyncio.IncompleteReadError):
                pass
            finally:
                await _close(w)

        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))


async def _reply(
    writer, status: int, reason: str, headers: dict[str, str] | None = None, body: str = ""
) -> None:
    data = body.encode()
    head = f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(data)}\r\nConnection: close\r\n"
    head += "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    with contextlib.suppress(OSError):
        writer.write(head.encode() + b"\r\n" + data)
        await writer.drain()
    await _close(writer)


async def _close(writer) -> None:
    with contextlib.suppress(OSError, RuntimeError):
        writer.close()
        await writer.wait_closed()
