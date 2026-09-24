"""End to end: a real node server on a local port, and the `tc wallet` commands talking to it."""

import socket
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn

from technocoin.cli import main
from technocoin.core.block import Block, block_from_bytes
from technocoin.core.params import REGTEST
from technocoin.core.pow import mine
from technocoin.node.api import create_app
from technocoin.node.service import NodeService
from technocoin.node.store import Store
from technocoin.wallet.keystore import INSECURE_FAST
from technocoin.wallet.wallet import Wallet

PARAMS = replace(REGTEST, coinbase_maturity=3)
PASSWORD = "password123"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def node_url(tmp_path):
    port = free_port()
    app = create_app(lambda: NodeService(PARAMS, Store(tmp_path / "chain.sqlite"), log=lambda line: None))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           ws="websockets-sansio"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server didn't start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def mine_to(url: str, address: str, count: int) -> None:
    with httpx.Client(base_url=url) as http:
        for _ in range(count):
            template = http.get("/v1/mining/template", params={"address": address}).json()
            block = block_from_bytes(bytes.fromhex(template["hex"]))
            mined = Block(mine(block.header, PARAMS.pow), block.transactions)
            assert http.post("/v1/mining/submit", json={"hex": mined.serialize().hex()}).json()["result"] == "new-tip"


def test_wallet_commands_against_a_running_node(tmp_path, node_url, monkeypatch, capsys):
    alice, _ = Wallet.create(tmp_path / "regtest" / "wallet.json", REGTEST, PASSWORD, strength=INSECURE_FAST)
    bob, _ = Wallet.create(tmp_path / "bob.json", REGTEST, PASSWORD, strength=INSECURE_FAST)
    alice_address, bob_address = alice.addresses[0].address, bob.addresses[0].address
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    base = ["--network", "regtest", "--datadir", str(tmp_path), "wallet", "--node", node_url]

    mine_to(node_url, alice_address, 4)  # block 1's reward is spendable; 2-4 are unlocking
    assert main(base + ["balance"]) == 0
    out = capsys.readouterr().out
    assert "Available now: 10 TC" in out and "unlocking over the next 3 blocks: 30 TC" in out

    assert main(base + ["send", bob_address, "2.5", "--memo", "thanks", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Fee:    0.000152 TC" in out  # 152 bytes (with the 6-byte memo) at the default 1 unit per byte
    assert "Sent. Transaction" in out

    assert main(base + ["history"]) == 0
    out = capsys.readouterr().out
    assert "-2.500152 TC  sent" in out and "waiting to be mined" in out

    mine_to(node_url, alice_address, 1)
    assert main(base + ["--file", str(tmp_path / "bob.json"), "balance"]) == 0
    assert "Available now: 2.5 TC" in capsys.readouterr().out

    # Spending more than is available is refused before anything is signed.
    assert main(base + ["send", bob_address, "1000", "--yes"]) == 1
    assert "not enough coins" in capsys.readouterr().err


def test_wallet_explains_when_no_node_is_running(tmp_path, capsys):
    Wallet.create(tmp_path / "regtest" / "wallet.json", REGTEST, PASSWORD, strength=INSECURE_FAST)
    args = ["--network", "regtest", "--datadir", str(tmp_path), "wallet", "--node", f"http://127.0.0.1:{free_port()}",
            "balance"]
    assert main(args) == 1
    assert "is `tc node` running?" in capsys.readouterr().err
