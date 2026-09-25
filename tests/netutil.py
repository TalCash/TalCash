"""Run several real nodes (API + peer-to-peer) on this machine, each on its own port."""

import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from talcash.core.block import Block, block_from_bytes
from talcash.core.params import NetworkParams
from talcash.core.pow import mine
from talcash.core.tx import Transfer
from talcash.node.api import ApiPolicy, create_app
from talcash.node.p2p import MAX_MESSAGE_BYTES, P2PConfig
from talcash.node.service import NodeService
from talcash.node.store import Store


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until(condition, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.1)


class LocalNode:
    def __init__(self, name: str, folder: Path, params: NetworkParams, peers: list["LocalNode"] = (),
                 policy: ApiPolicy = ApiPolicy()) -> None:
        self.name = name
        self.policy = policy
        self.folder = folder
        self.params = params
        self.port = free_port()
        self.peer_urls = [peer.p2p_url for peer in peers]
        self.log: list[str] = []
        self.server: uvicorn.Server | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def p2p_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/p2p"

    def start(self, peers: list["LocalNode"] | None = None, forget_peers: bool = False) -> "LocalNode":
        """`forget_peers` deletes the saved peer list (to really cut a node off)."""
        if peers is not None:
            self.peer_urls = [peer.p2p_url for peer in peers]
        self.folder.mkdir(parents=True, exist_ok=True)
        if forget_peers:
            (self.folder / "peers.json").unlink(missing_ok=True)
        app = create_app(
            lambda: NodeService(self.params, Store(self.folder / "chain.sqlite"), log=self.log.append),
            p2p=P2PConfig(listen_url=self.p2p_url, connect=self.peer_urls, peers_file=self.folder / "peers.json"),
            policy=self.policy,
        )
        self.app = app
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                ws="websockets-sansio", ws_max_size=MAX_MESSAGE_BYTES)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name=f"node-{self.name}")
        self.thread.start()
        wait_until(lambda: self.server.started, f"{self.name} to start", timeout=15)
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
            self.thread.join(timeout=15)
            self.server = None

    # --- asking the node -------------------------------------------------------

    def get(self, path: str, **params):
        return httpx.get(self.url + path, params=params, timeout=10).json()

    def status(self) -> dict:
        return self.get("/v1/status")

    def height(self) -> int:
        return self.status()["height"]

    def tip(self) -> str:
        return self.status()["tip"]["id"]

    def peer_count(self) -> int:
        return self.status()["peers"]

    def balance(self, address: str) -> str:
        return self.get(f"/v1/address/{address}")["balance"]

    def mempool(self) -> list[str]:
        return self.get("/v1/mempool")["txids"]

    def tx_status(self, txid: bytes) -> str | None:
        response = httpx.get(f"{self.url}/v1/tx/{txid.hex()}", timeout=10)
        return response.json()["status"] if response.status_code == 200 else None

    # --- acting ---------------------------------------------------------------

    def mine(self, address: str, count: int = 1) -> None:
        """Mine through this node's API (regtest proof of work is instant)."""
        with httpx.Client(base_url=self.url, timeout=10) as http:
            for _ in range(count):
                template = http.get("/v1/mining/template", params={"address": address}).json()
                block = block_from_bytes(bytes.fromhex(template["hex"]))
                mined = Block(mine(block.header, self.params.pow), block.transactions)
                result = http.post("/v1/mining/submit", json={"hex": mined.serialize().hex()}).json()
                assert result["result"] == "new-tip", result

    def send(self, tx: Transfer) -> None:
        response = httpx.post(f"{self.url}/v1/tx", json={"hex": tx.serialize().hex()}, timeout=10)
        assert response.status_code == 200, response.json()


# --- a hand-written peer, to send a node anything we like ------------------------------------


def raw_peer(node: LocalNode):
    """Connect to the node's peer-to-peer endpoint; returns the socket and the node's hello."""
    socket = connect(node.p2p_url, max_size=MAX_MESSAGE_BYTES, open_timeout=5)
    hello = json.loads(socket.recv(timeout=5))
    assert hello["type"] == "hello"
    return socket, hello


def say_hello(socket, hello: dict, **changes) -> str:
    """Answer the node's hello as a new node (a fresh node id). Returns the node id used."""
    node_id = changes.pop("node_id", os.urandom(16).hex())
    socket.send(json.dumps({**hello, "node_id": node_id, "listen": None, **changes}))
    return node_id


def receive_until(socket, kind: str, timeout: float = 10) -> tuple[dict, list[dict]]:
    """Read messages until one of type `kind`; returns it and everything received before it."""
    before = []
    deadline = time.monotonic() + timeout
    while True:
        message = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
        if message["type"] == kind:
            return message, before
        before.append(message)


def assert_disconnected(socket) -> None:
    """The node must hang up on us (going silent isn't enough)."""
    with pytest.raises(ConnectionClosed):
        for _ in range(10):
            socket.recv(timeout=10)  # it may send a message or two (e.g. get_peers) before hanging up


def assert_still_connected(socket) -> None:
    """The node still answers us."""
    socket.send(json.dumps({"type": "get_peers"}))
    receive_until(socket, "peers")
