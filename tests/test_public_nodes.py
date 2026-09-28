"""Seed nodes for `tc node`, and printing what a (possibly distant) node sends."""

import asyncio

import pytest

from talcash.cli import _shown, main
from talcash.core.params import REGTEST
from talcash.node.p2p import P2PConfig, PeerManager
from talcash.node.service import NodeService
from talcash.node.store import Store
from talcash.public_nodes import PUBLIC_API, SEEDS


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr("talcash.cli.run_node", lambda params, base, **options: calls.append(options))
    return calls


def test_a_testnet_node_finds_the_network_by_itself(tmp_path, started):
    base = ["--network", "testnet", "--datadir", str(tmp_path), "node"]
    assert main(base) == 0
    assert started[-1]["seeds"] == ["ws://testnet.talcash.com:64185/v1/p2p"] and started[-1]["peers"] == []

    assert main(base + ["--peer", "ws://friend.example:64185/v1/p2p"]) == 0  # your own choice replaces the seeds
    assert started[-1]["seeds"] == [] and started[-1]["peers"] == ["ws://friend.example:64185/v1/p2p"]

    assert main(base + ["--no-seeds"]) == 0
    assert started[-1]["seeds"] == []


def test_networks_without_public_nodes(tmp_path, started):
    for network in ("mainnet", "regtest"):  # mainnet's are added at launch
        assert main(["--network", network, "--datadir", str(tmp_path), "node"]) == 0
        assert started[-1]["seeds"] == []
    assert set(SEEDS) == set(PUBLIC_API) == {"testnet"}


def test_seeds_are_never_forgotten_but_not_trusted_with_private_addresses(monkeypatch):
    seed, learned = "ws://seed.example:64185/v1/p2p", "ws://learned.example:64185/v1/p2p"
    manager = PeerManager(NodeService(REGTEST, Store(":memory:"), log=lambda line: None), P2PConfig(seeds=[seed]))
    assert seed in manager.addresses and seed not in manager.private_addresses
    manager.addresses[learned] = None
    dialed = []

    async def unreachable(url, *, allow_private, **options):
        dialed.append((url, allow_private))
        raise OSError("connection refused")

    monkeypatch.setattr("talcash.node.p2p.connect_peer", unreachable)

    async def run():
        for _ in range(10):
            await manager._dial(seed)
            await manager._dial(learned)

    asyncio.run(run())
    assert seed in manager.addresses and learned not in manager.addresses
    assert (seed, False) in dialed


def test_a_node_can_see_its_own_seed_address(monkeypatch):
    """The public node itself: its seed is its own address, so it never dials itself."""
    url = SEEDS["testnet"][0]
    manager = PeerManager(NodeService(REGTEST, Store(":memory:"), log=lambda line: None),
                          P2PConfig(listen_url=url, seeds=[url]))
    monkeypatch.setattr(manager, "_spawn", lambda coroutine: (coroutine.close(), pytest.fail("dialed itself")))
    manager._dial_more()


def test_text_from_a_node_is_printed_without_control_codes():
    assert _shown("tt1abc\x1b[2J\x1b]0;owned\x07\r\n") == "tt1abc[2J]0;owned"
    assert _shown(1234) == "1234"
