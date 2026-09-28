"""Deterministic admission races and DNS destination checks; no external network calls."""

import asyncio
import socket
from types import SimpleNamespace

import pytest

from talcash.core.params import REGTEST
from talcash.node import peer_net
from talcash.node.api import ApiPolicy, create_app
from talcash.node.p2p import P2PConfig, PeerManager, _http_base, _valid_url
from talcash.node.service import NodeService
from talcash.node.store import Store


class PendingSocket:
    def __init__(self, app, host="8.8.8.8", fail=False):
        self.app = app
        self.client = SimpleNamespace(host=host, port=1234)
        self.started = asyncio.Event()
        self.fail = fail
        self.closed = None

    async def accept(self):
        self.started.set()
        if self.fail:
            raise OSError("handshake failed")
        await asyncio.Future()

    async def close(self, code=1000):
        self.closed = code

    async def send_text(self, text):
        pytest.fail("unaccepted peer cannot send")


def endpoint(kind, service):
    if kind == "p2p":
        manager = PeerManager(service, P2PConfig(max_inbound=1))
        return manager.accept, None
    app = create_app(lambda: service, policy=ApiPolicy(public=True, max_websockets=1))
    app.state.service = service
    return next(route.endpoint for route in app.routes if route.path == "/v1/ws"), app


@pytest.mark.parametrize("kind", ["api", "p2p"])
@pytest.mark.parametrize("other_host", ["8.8.8.8", "1.1.1.1"])
def test_pending_handshakes_occupy_capacity(kind, other_host):
    async def run():
        service = NodeService(REGTEST, Store(":memory:"), log=lambda _: None)
        handler, app = endpoint(kind, service)
        first = PendingSocket(app)
        task = asyncio.create_task(handler(first))
        try:
            await first.started.wait()
            second = PendingSocket(app, host=other_host)
            await asyncio.wait_for(handler(second), 1)
            assert second.closed == 1013
            assert not second.started.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Cancellation frees the reserved slot.
        again = PendingSocket(app, fail=True)
        with pytest.raises(OSError, match="handshake failed"):
            await handler(again)
        assert again.started.is_set()
        # A failed accept also frees it.
        retry = PendingSocket(app, fail=True)
        with pytest.raises(OSError, match="handshake failed"):
            await handler(retry)
        service.close()

    asyncio.run(run())


def answer(host, port=64185):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)


@pytest.mark.parametrize("destination", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1",
                                         "::ffff:127.0.0.1", "224.0.0.1", "100.64.0.1"])
def test_public_peer_dns_cannot_reach_nonpublic_addresses(monkeypatch, destination):
    async def run():
        async def lookup(*args, **kwargs):
            return [answer(destination)]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
        with pytest.raises(OSError, match="permitted destination"):
            await peer_net.resolve_peer("attacker.example", 64185, allow_private=False)
        assert await peer_net.resolve_peer("local.example", 64185, allow_private=True)
    asyncio.run(run())


def test_websocket_connect_uses_checked_ip_without_resolving_again(monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        lookups, connections = [], []

        async def lookup(host, port, **kwargs):
            lookups.append(host)
            return [answer("127.0.0.1"), answer("8.8.8.8")]

        class FakeSocket:
            def __init__(self, *args):
                self.closed = False
            def setblocking(self, value):
                pass
            def close(self):
                self.closed = True

        async def sock_connect(sock, destination):
            connections.append(destination)

        expected = object()
        async def handshake(url, *, sock, **kwargs):
            assert url == "wss://peer.example:64185/v1/p2p"
            assert not sock.closed
            return expected

        monkeypatch.setattr(loop, "getaddrinfo", lookup)
        monkeypatch.setattr(loop, "sock_connect", sock_connect)
        monkeypatch.setattr(peer_net.socket, "socket", FakeSocket)
        monkeypatch.setattr(peer_net, "_DirectConnect", handshake)
        assert await peer_net.connect_peer("wss://peer.example:64185/v1/p2p", allow_private=False) is expected
        assert lookups == ["peer.example"]
        assert connections == [("8.8.8.8", 64185)]
    asyncio.run(run())


def test_chunk_url_pins_ip_and_preserves_http_and_tls_hostname(monkeypatch):
    async def run():
        async def lookup(*args, **kwargs):
            return [answer("8.8.8.8", 443)]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
        target, headers, extensions = await peer_net.pinned_http_url(
            "https://peer.example/v1/chunks/0", allow_private=False
        )
        assert str(target) == "https://8.8.8.8/v1/chunks/0"
        assert headers == {"Host": "peer.example"}
        assert extensions == {"sni_hostname": "peer.example"}
    asyncio.run(run())


@pytest.mark.parametrize("url", ["ws://", "ws://[bad", "ws://host:99999/v1/p2p",
                                 "ws://user:pw@host/v1/p2p", "ws://host\\@127.0.0.1/",
                                 "ws://host/v1/p2p\n", "ws://host:0/v1/p2p"])
def test_malformed_peer_urls_are_rejected(url):
    assert not _valid_url(url)
    assert _http_base(url) is None


def test_websocket_redirects_do_not_open_another_connection():
    async def run():
        target_hits = []
        async def target(reader, writer):
            target_hits.append(True)
            writer.close()
            await writer.wait_closed()
        target_server = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = target_server.sockets[0].getsockname()[1]
        async def redirect(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write((f"HTTP/1.1 302 Found\r\nLocation: ws://127.0.0.1:{target_port}/v1/p2p\r\n"
                          "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        source = await asyncio.start_server(redirect, "127.0.0.1", 0)
        try:
            from websockets.exceptions import InvalidHandshake
            port = source.sockets[0].getsockname()[1]
            with pytest.raises(InvalidHandshake):
                await peer_net.connect_peer(f"ws://127.0.0.1:{port}/v1/p2p", allow_private=True)
            assert not target_hits
        finally:
            source.close()
            target_server.close()
            await source.wait_closed()
            await target_server.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["http-compression", "download-budget"])
def test_chunk_download_rejects_unbounded_responses(monkeypatch, failure):
    import httpx
    from talcash.node import p2p
    from talcash.node.blockfiles import ChunkTooLarge

    async def run():
        async def pin(*args, **kwargs):
            return httpx.URL("http://8.8.8.8/v1/chunks/0"), {"Host": "peer.example"}, {}

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * 11

        def respond(request):
            assert request.headers["accept-encoding"] == "identity"
            headers = {"content-encoding": "gzip"} if failure == "http-compression" else {}
            return httpx.Response(200, headers=headers, stream=Stream())

        real_client = httpx.AsyncClient
        def client(**kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            return real_client(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr(p2p, "pinned_http_url", pin)
        monkeypatch.setattr(p2p.httpx, "AsyncClient", client)
        monkeypatch.setattr(p2p, "MAX_SYNC_CHUNK_BYTES", 10)
        error = p2p.ProtocolError if failure == "http-compression" else ChunkTooLarge
        with pytest.raises(error):
            await p2p.download_chunk("http://peer.example", 0, 1000000)

    asyncio.run(run())


def test_chunk_download_checks_dns_before_creating_http_client(monkeypatch):
    from talcash.node import p2p

    async def run():
        async def lookup(*args, **kwargs):
            return [answer("127.0.0.1")]
        def forbidden(**kwargs):
            pytest.fail("private destination reached HTTP client")
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
        monkeypatch.setattr(p2p.httpx, "AsyncClient", forbidden)
        with pytest.raises(OSError, match="permitted destination"):
            await p2p.download_chunk("http://attacker.example", 0, 1000000)
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["api", "p2p"])
def test_pending_handshakes_reserve_per_host_slots(kind):
    async def run():
        service = NodeService(REGTEST, Store(":memory:"), log=lambda _: None)
        if kind == "p2p":
            handler, app = PeerManager(service, P2PConfig(max_inbound=32)).accept, None
        else:
            app = create_app(lambda: service, policy=ApiPolicy(public=True, websockets_per_host=4,
                                                              max_websockets=32))
            app.state.service = service
            handler = next(route.endpoint for route in app.routes if route.path == "/v1/ws")
        sockets = [PendingSocket(app) for _ in range(4)]
        tasks = [asyncio.create_task(handler(sock)) for sock in sockets]
        try:
            await asyncio.gather(*(sock.started.wait() for sock in sockets))
            refused = PendingSocket(app)
            await asyncio.wait_for(handler(refused), 1)
            assert refused.closed == 1013
            # Global capacity remains, and a different IP can still attempt a handshake.
            other = PendingSocket(app, host="1.1.1.1", fail=True)
            with pytest.raises(OSError, match="handshake failed"):
                await handler(other)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            service.close()
    asyncio.run(run())
