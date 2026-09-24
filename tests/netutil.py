"""Run several real nodes (API + peer-to-peer) on this machine, each on its own port."""

import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from technocoin.core.block import Block, block_from_bytes
from technocoin.core.params import NetworkParams
from technocoin.core.pow import mine
from technocoin.core.tx import Transfer
from technocoin.node.api import create_app
from technocoin.node.p2p import MAX_MESSAGE_BYTES, P2PConfig
from technocoin.node.service import NodeService
from technocoin.node.store import Store


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
    def __init__(self, name: str, folder: Path, params: NetworkParams, peers: list["LocalNode"] = ()) -> None:
        self.name = name
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
        )
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
