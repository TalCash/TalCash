"""Resolve peer destinations once and connect only to the checked addresses."""

import asyncio
import ipaddress
import socket
from urllib.parse import SplitResult, urlsplit

import httpx
from websockets.asyncio.client import connect


def parse_peer_url(url: str) -> SplitResult:
    if (not isinstance(url, str) or len(url) > 200
            or any(c.isspace() or ord(c) < 32 or c == '\\' for c in url)):
        raise ValueError("invalid peer URL")
    parsed = urlsplit(url)
    if (parsed.scheme not in ("ws", "wss") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.query or '%' in parsed.hostname
            or parsed.port == 0):
        raise ValueError("invalid peer URL")
    return parsed


def public_address(host: str) -> bool:
    address = ipaddress.ip_address(host)
    address = getattr(address, "ipv4_mapped", None) or address
    return address.is_global and not address.is_multicast


async def resolve_peer(host: str, port: int, *, allow_private: bool) -> list[tuple]:
    loop = asyncio.get_running_loop()
    answers = await asyncio.wait_for(
        loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=5
    )
    allowed = []
    for answer in answers:
        family, _, _, _, destination = answer
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        if allow_private or public_address(destination[0]):
            if answer not in allowed:
                allowed.append(answer)
    if not allowed:
        raise OSError("peer has no permitted destination addresses")
    return allowed


class _DirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # A Location header is untrusted too; the approved socket is the only destination.
        return exc


async def connect_peer(url: str, *, allow_private: bool, **kwargs):
    parsed = parse_peer_url(url)
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    async with asyncio.timeout(5):
        addresses = await resolve_peer(parsed.hostname, port, allow_private=allow_private)
        loop = asyncio.get_running_loop()
        last_error = None
        for family, kind, protocol, _, destination in addresses:
            sock = socket.socket(family, kind, protocol)
            sock.setblocking(False)
            try:
                # destination contains a numeric IP, so it cannot be resolved a second time.
                await loop.sock_connect(sock, destination)
            except OSError as error:
                sock.close()
                last_error = error
                continue
            except BaseException:
                sock.close()
                raise
            try:
                # Retain the original hostname for Host, TLS SNI and certificate checks.
                # Supplying a socket also bypasses environment proxy discovery.
                return await _DirectConnect(url, sock=sock, **kwargs)
            except BaseException:
                sock.close()
                raise
        raise last_error or OSError("peer connection failed")


async def pinned_http_url(url: str, *, allow_private: bool) -> tuple[httpx.URL, dict, dict]:
    original = httpx.URL(url)
    if original.scheme not in ("http", "https") or not original.host or original.userinfo:
        raise ValueError("invalid peer HTTP URL")
    addresses = await resolve_peer(original.host, original.port or (443 if original.scheme == "https" else 80),
                                   allow_private=allow_private)
    target = original.copy_with(host=addresses[0][4][0])
    # The URL pins TCP to an approved IP; HTTP and TLS still use the original authority.
    return target, {"Host": original.netloc.decode("ascii")}, {"sni_hostname": original.host}
