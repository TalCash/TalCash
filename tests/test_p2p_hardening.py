"""Peers that cheat, flood or lie: the node must protect itself and carry on."""

import json
import os
import time
from dataclasses import replace

import pytest
from websockets.exceptions import ConnectionClosed

from talcash.core.block import BlockHeader
from talcash.core.genesis import genesis_block
from talcash.core.params import REGTEST
from talcash.core.pow import meets_target, mine
from talcash.crypto.address import encode_address
from talcash.node import p2p
from talcash.node.p2p import P2PConfig, Peer, PeerManager
from talcash.node.service import NodeService
from talcash.node.store import Store

from chainutil import TestChain, make_transfer, named_key
from netutil import (LocalNode, assert_disconnected, assert_still_connected, raw_peer, receive_until,
                     say_hello, wait_until)

PARAMS = replace(REGTEST, coinbase_maturity=3, finality_depth=50)
GENESIS = genesis_block(PARAMS).header
ALICE, BOB = named_key("alice"), named_key("bob")
HUGE_WORK = "f" * 60  # more work than any test chain: makes the node catch up from us


def text(key) -> str:
    return encode_address(key.address, PARAMS.address_prefix)


@pytest.fixture
def node(tmp_path):
    started = LocalNode("a", tmp_path / "a", PARAMS).start()
    yield started
    started.stop()


def header_chain(parent: BlockHeader, count: int, *, valid_pow: bool = True) -> list[BlockHeader]:
    """Headers following `parent` that obey every header rule (except proof of work, if asked)."""
    headers = []
    for _ in range(count):
        header = BlockHeader(version=1, height=parent.height + 1, prev_id=parent.block_id,
                             merkle_root=os.urandom(32), snapshot_root=bytes(32),
                             timestamp=parent.timestamp + 1, target=PARAMS.genesis_target, nonce=0)
        if valid_pow:
            header = mine(header, PARAMS.pow)
        else:
            header = replace(header, nonce=next(n for n in range(10_000)
                                                if not meets_target(replace(header, nonce=n), PARAMS.pow)))
        headers.append(header)
        parent = header
    return headers


def send(socket, message: dict) -> None:
    socket.send(json.dumps(message))


def headers_message(headers: list[BlockHeader]) -> dict:
    return {"type": "headers", "headers": [h.serialize().hex() for h in headers]}


def everything_until_closed(socket) -> list[dict]:
    received = []
    with pytest.raises(ConnectionClosed):
        while True:
            received.append(json.loads(socket.recv(timeout=10)))
    return received


# --- headers first --------------------------------------------------------------------


def test_headers_with_fake_proof_of_work_get_the_peer_banned_before_any_block_is_downloaded(node):
    node.mine(text(ALICE), 3)
    socket, hello = raw_peer(node)
    node_id = say_hello(socket, hello, work=HUGE_WORK, height=1000)  # "I have a much better chain"
    receive_until(socket, "get_headers")  # so the node asks us for headers
    send(socket, headers_message(header_chain(GENESIS, 20, valid_pow=False)))
    received = everything_until_closed(socket)
    assert not [m for m in received if m["type"] == "get_data" and m.get("blocks")]
    assert any("banned" in line and "proof of work" in line for line in node.log)
    assert node.height() == 3

    socket, hello = raw_peer(node)  # the same node can't come straight back
    say_hello(socket, hello, node_id=node_id)
    assert_disconnected(socket)


def test_a_chain_with_less_work_is_not_downloaded(node):
    node.mine(text(ALICE), 20)
    socket, hello = raw_peer(node)
    say_hello(socket, hello, work=HUGE_WORK)  # a lie: our headers will add up to less than the node's chain
    receive_until(socket, "get_headers")
    send(socket, headers_message(header_chain(GENESIS, 10)))
    wait_until(lambda: any("no more work than ours" in line for line in node.log), "the node to see through it")
    send(socket, {"type": "get_peers"})
    _, before = receive_until(socket, "peers")
    assert not [m for m in before if m["type"] == "get_data" and m.get("blocks")]
    assert node.height() == 20  # and we're still connected: headers with real work are no crime


def test_a_chain_with_more_work_is_downloaded_once_its_headers_check_out(node):
    node.mine(text(ALICE), 20)
    socket, hello = raw_peer(node)
    say_hello(socket, hello, work=HUGE_WORK)
    receive_until(socket, "get_headers")
    heavier = header_chain(GENESIS, 25)
    send(socket, headers_message(heavier))
    request, _ = receive_until(socket, "get_data")
    assert request["blocks"][:3] == [h.block_id.hex() for h in heavier[:3]]  # in order, from the split


def test_a_block_whose_parent_is_unknown_starts_a_catch_up_instead_of_waiting_in_memory(node):
    ahead = TestChain(PARAMS)
    ahead.mine_blocks(3)
    socket, hello = raw_peer(node)
    say_hello(socket, hello)
    send(socket, {"type": "block", "hex": ahead.blocks[3].serialize().hex()})  # the node lacks block 2
    receive_until(socket, "get_headers")
    assert node.app.state.service.chain._orphans == {}


# --- misbehaviour and limits -------------------------------------------------------------


def test_a_forged_transfer_gets_the_peer_banned_but_one_that_is_merely_early_does_not(node):
    node.mine(text(ALICE), 4)
    socket, hello = raw_peer(node)
    say_hello(socket, hello)

    early = make_transfer(PARAMS, ALICE, 5, [(BOB.address, 1000)], fee=500)  # nonces 0-4 haven't happened yet
    send(socket, {"type": "tx", "hex": early.serialize().hex()})
    assert_still_connected(socket)

    forged = replace(make_transfer(PARAMS, ALICE, 0, [(BOB.address, 1000)], fee=500), fee=600)  # signature broken
    send(socket, {"type": "tx", "hex": forged.serialize().hex()})
    assert_disconnected(socket)
    assert any("banned" in line and "bad-signature" in line for line in node.log)


def test_unknown_messages_are_ignored(node):
    socket, hello = raw_peer(node)
    say_hello(socket, hello)
    send(socket, {"type": "some-future-feature", "data": [1, 2, 3]})
    assert_still_connected(socket)


def test_a_flooding_peer_is_slowed_down_not_served_faster(node, monkeypatch):
    monkeypatch.setattr(p2p, "MESSAGE_RATE", 20)  # budget units per second
    monkeypatch.setattr(p2p, "MESSAGE_BURST", 100)
    socket, hello = raw_peer(node)
    say_hello(socket, hello)
    for _ in range(40):
        send(socket, {"type": "get_peers"})  # 50 units each
    answers, deadline = 0, time.monotonic() + 3
    while (left := deadline - time.monotonic()) > 0:
        try:
            answers += json.loads(socket.recv(timeout=left))["type"] == "peers"
        except TimeoutError:
            break
        started = time.monotonic()
        node.status()
        assert time.monotonic() - started < 1  # everyone else is served as usual meanwhile
    # Two at once (100 saved up), then one every 2.5 seconds.
    assert 2 <= answers <= 4
    socket.close()


def manager() -> PeerManager:
    return PeerManager(NodeService(PARAMS, Store(":memory:"), log=lambda line: None), P2PConfig())


def fake_peer(host: str, node_id: str = "") -> Peer:
    peer = Peer(None, None, None, outbound=False, label=host, host=host)
    peer.node_id = node_id
    return peer


def test_a_ban_covers_the_node_id_and_the_address_but_not_this_computer():
    peers = manager()
    peers._ban(fake_peer("203.0.113.7", "aa" * 16))
    assert "aa" * 16 in peers.banned
    assert peers.inbound_refusal("203.0.113.7") == 1008  # a new node id from there doesn't help
    assert peers.inbound_refusal("203.0.113.8") is None

    peers._ban(fake_peer("127.0.0.1", "bb" * 16))
    assert "bb" * 16 in peers.banned  # that node is out...
    assert peers.inbound_refusal("127.0.0.1") is None  # ...but other nodes on this computer aren't

    peers.banned_hosts["203.0.113.7"] = peers.banned["aa" * 16] = time.monotonic() - 1  # an hour later
    peers._forget_expired_bans()
    assert peers.inbound_refusal("203.0.113.7") is None and "aa" * 16 not in peers.banned


def test_inbound_connections_per_address_are_limited():
    peers = manager()
    for _ in range(p2p.MAX_INBOUND_PER_HOST):
        peers.peers.add(fake_peer("203.0.113.9"))
        peers.peers.add(fake_peer("127.0.0.1"))
    assert peers.inbound_refusal("203.0.113.9") == 1013
    assert peers.inbound_refusal("203.0.113.10") is None
    assert peers.inbound_refusal("127.0.0.1") is None  # local nodes (a devnet) aren't limited


def test_only_fully_checked_blocks_are_passed_on_and_they_are_read_when_sent():
    peers = manager()
    chain = peers.service.chain
    main = TestChain(PARAMS)
    main.mine_blocks(2)
    side = TestChain(PARAMS)
    side.mine_blocks(1, miner=BOB.address)  # a rival block 1: stored, but never connected or fully checked
    for block in (main.blocks[1], side.blocks[1], main.blocks[2]):
        chain.submit_block(block)
    peer = fake_peer("203.0.113.1")
    wanted = [main.blocks[2].block_id, side.blocks[1].block_id, os.urandom(32)]
    peers._on_get_data(peer, {"type": "get_data", "blocks": [b.hex() for b in wanted], "txs": []})

    lazy, not_found = peer.queue.get_nowait(), json.loads(peer.queue.get_nowait())
    assert callable(lazy)  # nothing read from disk yet
    assert json.loads(lazy())["hex"] == main.blocks[2].serialize().hex()
    assert not_found == {"type": "not_found", "blocks": [b.hex() for b in wanted[1:]], "txs": []}


def test_addresses_on_this_computer_are_only_passed_on_to_local_peers():
    peers = manager()
    for url in ("ws://127.0.0.1:64195/v1/p2p", "ws://192.168.1.20:64185/v1/p2p", "ws://85.10.20.30:64185/v1/p2p"):
        peers.addresses[url] = None
    stranger, neighbour = fake_peer("207.180.243.13"), fake_peer("127.0.0.1")
    peers._on_get_peers(stranger, {"type": "get_peers"})
    peers._on_get_peers(neighbour, {"type": "get_peers"})
    assert json.loads(stranger.queue.get_nowait())["urls"] == ["ws://85.10.20.30:64185/v1/p2p"]
    assert len(json.loads(neighbour.queue.get_nowait())["urls"]) == 3


def test_local_addresses_from_peers_elsewhere_are_ignored():
    """Otherwise a stranger could make the node connect to services on its own machine or network."""
    peers = manager()
    stranger = fake_peer("85.10.20.30")
    peers._on_peers(stranger, {"type": "peers", "urls": [
        "ws://127.0.0.1:64185/v1/p2p", "ws://10.0.0.5:8080/v1/p2p", "ws://85.10.20.31:64185/v1/p2p"]})
    assert list(peers.addresses) == ["ws://85.10.20.31:64185/v1/p2p"]

    claims_local = fake_peer("85.10.20.32")  # says "reach me at 127.0.0.1": that would be ourselves
    peers._handle_hello(claims_local, {**peers._hello(), "node_id": "cc" * 16, "listen": "ws://127.0.0.1:9/v1/p2p"})
    assert claims_local.http_url is None  # no chunk downloads from our own machine either
    assert "ws://127.0.0.1:9/v1/p2p" not in peers.addresses

    neighbour = fake_peer("127.0.0.1")  # a node on this very computer may tell us about local ones
    peers._on_peers(neighbour, {"type": "peers", "urls": ["ws://127.0.0.1:64195/v1/p2p"]})
    assert "ws://127.0.0.1:64195/v1/p2p" in peers.addresses
