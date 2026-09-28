"""A public node behind a reverse proxy on the same computer (Caddy adding HTTPS), and browser access (CORS)."""

import asyncio
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from talcash.core.params import REGTEST
from talcash.node.api import ApiPolicy, create_app
from talcash.node.server import LOCAL_PROXIES, server_config
from talcash.node.service import NodeService
from talcash.node.store import Store
from test_wallet_live import free_port


def new_service() -> NodeService:
    return NodeService(REGTEST, Store(":memory:"), log=lambda line: None)


@pytest.fixture
def proxied_node(monkeypatch):
    """A public-mode node started the way `tc node` starts it. The environment asks uvicorn to
    believe X-Forwarded-For from anyone; the node must ignore that."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    port = free_port()
    policy = ApiPolicy(public=True, requests_per_second=0.01, burst=3, websockets_per_host=1)
    config = server_config(create_app(new_service, policy=policy), "127.0.0.1", port, public=True)
    assert config.forwarded_allow_ips == LOCAL_PROXIES
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server didn't start"
        time.sleep(0.05)
    yield f"127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def via_proxy(visitor: str) -> dict:
    return {"X-Forwarded-For": visitor}


def test_visitors_through_a_local_proxy_are_strangers(proxied_node):
    with httpx.Client(base_url=f"http://{proxied_node}") as http:
        # This computer itself (e.g. the block explorer, or the miner) keeps full access...
        assert http.get("/v1/mining/template", params={"address": "tr1x"}).status_code == 400  # bad address, not 403
        assert {http.get("/v1/status").status_code for _ in range(10)} == {200}
        # ...but a visitor the proxy passes on is a stranger: no mining, and a rate limit of its own.
        response = http.get("/v1/mining/template", params={"address": "tr1x"}, headers=via_proxy("198.51.100.7"))
        assert response.status_code == 403 and response.json()["error"] == "local-only"
        codes = [http.get("/v1/status", headers=via_proxy("198.51.100.7")).status_code for _ in range(3)]
        assert codes == [200, 200, 429]  # 3 in the bucket, one used on the mining request
        assert http.get("/v1/status", headers=via_proxy("192.0.2.200")).status_code == 200  # another visitor
        # A proxy appends the address it saw; whatever the visitor wrote before that is ignored.
        spoofed = via_proxy("127.0.0.1, 192.0.2.201")
        assert http.get("/v1/mining/template", params={"address": "tr1x"}, headers=spoofed).status_code == 403


def test_websocket_limits_apply_per_visitor(proxied_node):
    async def run():
        url = f"ws://{proxied_node}/v1/ws"
        async with connect(url, additional_headers=via_proxy("198.51.100.8")):
            with pytest.raises(InvalidStatus):  # a second one from the same visitor is refused
                async with connect(url, additional_headers=via_proxy("198.51.100.8")) as second:
                    await asyncio.wait_for(second.recv(), 2)  # accepted by mistake: fail, don't hang
            async with connect(url, additional_headers=via_proxy("198.51.100.9")) as other:  # others still get in
                await other.send('{"subscribe": ["blocks"]}')
                assert "subscribed" in await asyncio.wait_for(other.recv(), 5)

    asyncio.run(run())


def test_browser_pages_can_use_a_public_node():
    with TestClient(create_app(new_service, policy=ApiPolicy(public=True, requests_per_second=0.01, burst=2))) as api:
        origin = {"Origin": "https://wallet.example"}
        assert api.get("/v1/status", headers=origin).headers["access-control-allow-origin"] == "*"
        preflight = api.options("/v1/tx", headers={**origin, "Access-Control-Request-Method": "POST",
                                                   "Access-Control-Request-Headers": "content-type"})
        assert preflight.status_code == 200 and "POST" in preflight.headers["access-control-allow-methods"]
        assert "access-control-allow-credentials" not in preflight.headers
        api.get("/v1/status", headers=origin)
        limited = api.get("/v1/status", headers=origin)
        assert limited.status_code == 429 and limited.headers["access-control-allow-origin"] == "*"  # readable
        assert "retry-after" in limited.headers["access-control-expose-headers"].lower()


def test_a_private_node_is_not_open_to_web_pages():
    with TestClient(create_app(new_service)) as api:
        response = api.get("/v1/status", headers={"Origin": "https://evil.example"})
        assert response.status_code == 200 and "access-control-allow-origin" not in response.headers
