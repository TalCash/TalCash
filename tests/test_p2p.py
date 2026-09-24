"""Several real nodes on this machine, talking to each other over the peer-to-peer protocol."""

import json
import os
import time
from dataclasses import replace

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from technocoin.core.block import Block, block_from_bytes
from technocoin.core.params import REGTEST
from technocoin.core.pow import meets_target
from technocoin.crypto.address import encode_address
from technocoin.node import p2p
from technocoin.node.network import join_devnet, load_params

from chainutil import make_transfer, named_key
from netutil import LocalNode, wait_until

PARAMS = replace(REGTEST, coinbase_maturity=3, finality_depth=50)
ALICE, BOB, CAROL = named_key("alice"), named_key("bob"), named_key("carol")


def text(key) -> str:
    return encode_address(key.address, PARAMS.address_prefix)


@pytest.fixture
def nodes(tmp_path):
    started: list[LocalNode] = []

    def make(name: str, peers: list[LocalNode] = (), start: bool = True, params=PARAMS) -> LocalNode:
        node = LocalNode(name, tmp_path / name, params, list(peers))
        started.append(node)
        return node.start() if start else node

    yield make
    for node in started:
        node.stop()


def same_tip(*nodes: LocalNode) -> bool:
    return len({node.tip() for node in nodes}) == 1


def test_blocks_spread_between_two_nodes(nodes):
    a = nodes("a")
    b = nodes("b", peers=[a])
    wait_until(lambda: a.peer_count() == 1 and b.peer_count() == 1, "the nodes to connect")
    a.mine(text(ALICE), 5)
    wait_until(lambda: b.height() == 5 and same_tip(a, b), "b to receive a's blocks")
    b.mine(text(BOB), 3)  # and the other way round
    wait_until(lambda: a.height() == 8 and same_tip(a, b), "a to receive b's blocks")


def test_a_late_node_catches_up_in_batches(nodes, monkeypatch):
    monkeypatch.setattr(p2p, "MAX_HEADERS", 40)  # force several header rounds...
    monkeypatch.setattr(p2p, "BLOCKS_PER_REQUEST", 7)  # ...and many download batches
    a = nodes("a")
    a.mine(text(ALICE), 150)
    late = nodes("late", peers=[a])
    wait_until(lambda: late.height() == 150 and same_tip(a, late), "the late node to catch up")
    assert late.balance(text(ALICE)) == a.balance(text(ALICE)) == "1470.000000"  # 147 matured rewards
    assert any("caught up" in line for line in late.log)


def test_payments_and_blocks_travel_along_a_line_of_nodes(nodes):
    """a - b - c: a and c aren't connected, everything goes through b."""
    a = nodes("a")
    b = nodes("b", peers=[a])
    c = nodes("c", peers=[b])
    wait_until(lambda: b.peer_count() == 2, "b to connect to both")
    a.mine(text(ALICE), 4)
    wait_until(lambda: c.height() == 4, "c to receive a's blocks through b")

    tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 3_000_000), (CAROL.address, 1_000_000)], fee=500)
    c.send(tx)  # submitted at c, mined by a
    wait_until(lambda: tx.txid.hex() in a.mempool(), "the payment to reach a through b")
    a.mine(text(ALICE))
    wait_until(lambda: c.tx_status(tx.txid) == "confirmed", "c to see the payment confirmed")
    for node in (a, b, c):
        assert node.balance(text(BOB)) == "3.000000" and node.balance(text(CAROL)) == "1.000000"
        assert node.mempool() == []


def test_split_networks_rejoin_on_the_chain_with_more_work(nodes):
    a = nodes("a")
    b = nodes("b", peers=[a])
    a.mine(text(ALICE), 4)  # shared history: Alice has 10 TC spendable
    wait_until(lambda: b.height() == 4, "b to sync the shared history")

    # Cut the network in two: restart both without peers (and without their saved peer lists).
    a.stop()
    b.stop()
    a.start(peers=[], forget_peers=True)
    b.start(peers=[], forget_peers=True)
    to_carol = make_transfer(PARAMS, ALICE, 0, [(CAROL.address, 2_000_000)], fee=500)
    a.send(to_carol)
    a.mine(text(ALICE), 1)  # a's side: height 5, includes the payment
    b.mine(text(BOB), 3)  # b's side: height 7, more work
    assert a.tx_status(to_carol.txid) == "confirmed" and b.tx_status(to_carol.txid) is None

    # Reconnect: a switches to b's heavier chain; its block 5 is undone, so the payment goes back to
    # waiting, a passes it to b, and it gets mined again on the winning chain.
    a.stop()
    a.start(peers=[b])
    wait_until(lambda: same_tip(a, b) and a.height() == 7, "a to switch to b's chain")
    assert any("chain switch" in line for line in a.log)
    wait_until(lambda: to_carol.txid.hex() in b.mempool(), "the undone payment to reach b")
    b.mine(text(BOB))
    wait_until(lambda: a.tx_status(to_carol.txid) == "confirmed" and same_tip(a, b), "the payment to confirm")
    assert a.balance(text(CAROL)) == b.balance(text(CAROL)) == "2.000000"


def raw_peer(node: LocalNode):
    """A hand-written peer connection, to send the node anything we like."""
    socket = connect(node.p2p_url, max_size=p2p.MAX_MESSAGE_BYTES, open_timeout=5)
    hello = json.loads(socket.recv(timeout=5))
    assert hello["type"] == "hello"
    return socket, hello


def assert_disconnected(socket) -> None:
    """The node must hang up on us (going silent isn't enough)."""
    with pytest.raises(ConnectionClosed):
        for _ in range(10):
            socket.recv(timeout=10)  # it may send a message or two (e.g. get_peers) before hanging up


def test_bad_peers_are_disconnected_and_the_node_carries_on(nodes):
    a = nodes("a")
    a.mine(text(ALICE), 2)

    socket, _ = raw_peer(a)
    socket.send("this is not json")
    assert_disconnected(socket)

    socket, hello = raw_peer(a)
    socket.send(json.dumps({**hello, "network": "mainnet", "node_id": os.urandom(16).hex()}))
    assert_disconnected(socket)

    socket, _ = raw_peer(a)
    socket.send(b"\x00binary")
    assert_disconnected(socket)

    # A proper hello, then a block with invalid proof of work.
    socket, hello = raw_peer(a)
    socket.send(json.dumps({**hello, "node_id": os.urandom(16).hex(), "listen": None}))
    template = block_from_bytes(bytes.fromhex(a.get("/v1/mining/template", address=text(BOB))["hex"]))
    nonce = next(n for n in range(1000) if not meets_target(replace(template.header, nonce=n), PARAMS.pow))
    forged = Block(replace(template.header, nonce=nonce), template.transactions)
    socket.send(json.dumps({"type": "block", "hex": forged.serialize().hex()}))
    assert_disconnected(socket)

    assert a.height() == 2  # nothing got in, and the node still answers
    assert a.peer_count() == 0


# Days of 5 blocks, final 10 deep: at height 60, days 0-9 (heights 0-49) are sealed chunk files.
CHUNKY = replace(PARAMS, chunk_size=5, snapshot_delay=2, finality_depth=10)


def test_a_late_node_catches_up_with_chunk_files(nodes):
    a = nodes("a", params=CHUNKY)
    a.mine(text(ALICE), 60)
    assert a.status()["sealed_chunks"] == 10
    late = nodes("late", peers=[a], params=CHUNKY)
    wait_until(lambda: late.height() == 60 and same_tip(a, late), "the late node to catch up")
    imported = [line for line in late.log if "imported chunk" in line]
    assert len(imported) == 10  # heights 0-49 came as ten chunk files; 50-60 block by block
    assert late.status()["sealed_chunks"] == 10
    assert late.balance(text(ALICE)) == a.balance(text(ALICE))


def test_a_damaged_chunk_gets_the_peer_dropped_and_another_peer_is_used(nodes, monkeypatch):
    a = nodes("a", params=CHUNKY)
    a.mine(text(ALICE), 60)
    b = nodes("b", peers=[a], params=CHUNKY)
    wait_until(lambda: b.height() == 60 and b.status()["sealed_chunks"] == 10, "b to catch up")

    real_download, calls = p2p.download_chunk, []

    async def first_download_damaged(http_base, index):
        data = await real_download(http_base, index)
        calls.append(http_base)
        if len(calls) == 1:
            data = data[:-1] + bytes([data[-1] ^ 1])  # break the checksum
        return data

    monkeypatch.setattr(p2p, "download_chunk", first_download_damaged)
    late = nodes("late", peers=[a, b], params=CHUNKY)
    wait_until(lambda: late.height() == 60 and same_tip(a, late), "the late node to catch up anyway")
    assert any("damaged chunk" in line for line in late.log)
    assert len({url for url in calls}) == 2  # it switched to the other peer
    assert late.peer_count() == 1  # the peer that sent the damaged file stays disconnected


def test_a_restarted_node_finds_the_network_again(nodes, tmp_path):
    a = nodes("a")
    b = nodes("b", peers=[a])
    c = nodes("c", peers=[b])  # c only knows b; b tells it about a
    wait_until(lambda: c.peer_count() == 2, "c to learn about a from b and connect to it")
    c.stop()
    saved = json.loads((tmp_path / "c" / "peers.json").read_text())
    assert set(saved) == {a.p2p_url, b.p2p_url}

    c.start(peers=[])  # no --peer this time
    wait_until(lambda: c.peer_count() == 2, "c to reconnect to both from its saved list")
    a.mine(text(ALICE), 2)
    wait_until(lambda: c.height() == 2, "c to receive new blocks")


def test_a_new_devnet_node_joins_an_existing_devnet(tmp_path):
    params = load_params("devnet", tmp_path / "first")  # a brand-new devnet (fresh genesis)
    first = LocalNode("first", tmp_path / "first-node", params).start()
    try:
        join_devnet(first.p2p_url, tmp_path / "second")
        assert load_params("devnet", tmp_path / "second", create=False).genesis_id == params.genesis_id
        with pytest.raises(RuntimeError, match="isn't a devnet"):
            regtest_node = LocalNode("regtest", tmp_path / "regtest-node", PARAMS).start()
            try:
                join_devnet(regtest_node.p2p_url, tmp_path / "third")
            finally:
                regtest_node.stop()
    finally:
        first.stop()


def test_a_node_listed_as_its_own_peer_ignores_itself(nodes):
    lonely = nodes("lonely", start=False)
    lonely.start(peers=[lonely])
    lonely.mine(text(ALICE), 1)
    time.sleep(3)
    assert lonely.peer_count() == 0
    assert not any("connected to" in line for line in lonely.log)
