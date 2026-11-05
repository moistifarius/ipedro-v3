"""Keep the bot from being used to reach the operator's own network.

Several features download a URL that came from somewhere the operator
doesn't control: a Reddit post's link (any member can plant one), an image
search result. The bot runs on a home server next to everything else, so
`http://192.168.1.10/snapshot.jpg` as a "meme" would have the server fetch
a LAN camera and post the picture into the chat, and `http://169.254.169.254/`
or `http://localhost:...` reach services that trust anything on the box.

assert_public() refuses a URL that isn't plain http(s) to a public address.
The httpx hook applies it to the first request and to every redirect hop,
since a public URL that 302s to a private one is the usual way past a
check made only once.

Known limit: the name is resolved here and again when httpx connects, so a
hostile DNS server answering differently the second time (rebinding) can
still slip through. That needs an attacker running DNS for a name the bot
is asked to fetch, and a LAN target worth the trouble; closing it means
pinning the resolved address into the connection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import httpx


_SECRET_PARAM_RE = re.compile(
    r"(?i)([?&;](?:password|passwd|pass|pwd|token|key|secret|apikey|api_key)=)[^&#\s]*")
_USERINFO_RE = re.compile(r"(?i)(^[a-z][a-z0-9+.-]*://)[^/@\s]+@")


def redact_url(url: str | None) -> str | None:
    """`url` with anything secret-looking blanked, for logs and for replies
    that show a configured URL back. A private KiwiSDR takes its password in
    the query string (kiwi://host:8073?freq=...&password=...), and a stream URL
    may carry `user:pass@`; both used to be written to the container log on
    every refresh and printed by /ether_status."""
    if not url:
        return url
    return _USERINFO_RE.sub(r"\1***@", _SECRET_PARAM_RE.sub(r"\1***", url))


class UnsafeURL(ValueError):
    """The URL points somewhere the bot must not fetch from."""


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    # IPv4-mapped IPv6 (::ffff:192.168.1.10) is judged as the IPv4 it wraps.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global


async def assert_public(url: str) -> None:
    """Raise UnsafeURL unless `url` is http(s) to a public address."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL(f"scheme {parts.scheme!r} is not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURL("no host")
    if parts.username or parts.password:
        raise UnsafeURL("credentials in the URL")
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(
        (".local", ".internal", ".localhost", ".lan", ".home.arpa")
    ):
        raise UnsafeURL(f"{host} is a local name")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    # A literal address needs no lookup.
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        try:
            addrs = await _resolve(host, port)
        except OSError as exc:
            raise UnsafeURL(f"can't resolve {host}") from exc
    if not addrs:
        raise UnsafeURL(f"{host} resolves to nothing")
    bad = [a for a in addrs if not _is_public(a)]
    if bad:
        raise UnsafeURL(f"{host} resolves to a non-public address ({bad[0]})")


async def _check_request(request: httpx.Request) -> None:
    await assert_public(str(request.url))


# For httpx.AsyncClient(event_hooks=PUBLIC_ONLY): runs before the request and
# before every redirect hop.
PUBLIC_ONLY = {"request": [_check_request]}
