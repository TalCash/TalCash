import copy
import random
from dataclasses import replace

import pytest

from technocoin.core.block import Block
from technocoin.core.errors import ValidationError
from technocoin.core.params import REGTEST
from technocoin.core.pow import mine
from technocoin.core.tx import Coinbase
from technocoin.node.chain import ChainManager, Outcome
from technocoin.node.mempool import Mempool
from technocoin.node.store import Store
from technocoin.node.template import build_template

from chainutil import TestChain, make_transfer, named_key

PARAMS = replace(REGTEST, coinbase_maturity=3, chunk_size=5, snapshot_delay=2, finality_depth=10)
NOW = 2_000_000_000
TC = 1_000_000
ALICE, BOB, CAROL, MINER = named_key("alice"), named_key("bob"), named_key("carol"), named_key("miner")


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def setup(**pool_options):
    """A node where Alice has 20 TC and Bob 10 TC confirmed (at height 6)."""
    ref = TestChain(PARAMS)
    ref.mine_blocks(2, miner=ALICE.address)
    ref.mine_blocks(1, miner=BOB.address)
    ref.mine_blocks(3, miner=MINER.address)
    node = ChainManager(Store(":memory:"), PARAMS, clock=lambda: NOW)
    for block in ref.blocks[1:]:
        node.submit_block(block)
    pool = Mempool(PARAMS, node.store, clock=pool_options.pop("clock", lambda: NOW), **pool_options)
    return ref, node, pool


def pay(sender, nonce, amount=TC, to=BOB, fee=200):
    return make_transfer(PARAMS, sender, nonce, [(to.address, amount)], fee=fee)


def mine_template(node: ChainManager, pool: Mempool) -> Block:
    template = build_template(node, pool, MINER.address, now=node.tip.header.timestamp + 60)
    return Block(mine(template.header, PARAMS.pow), template.transactions)


def test_accepts_transfers_and_tracks_nonces():
    _, _, pool = setup()
    first, second = pay(ALICE, 0), pay(ALICE, 1)
    pool.add(first)
    pool.add(second)
    assert len(pool) == 2 and first.txid in pool
    assert pool.next_nonce(ALICE.address) == 2
    assert pool.next_nonce(CAROL.address) == 0
    with pytest.raises(ValidationError, match="already-known"):
        pool.add(first)


def test_rejects_nonce_gaps_and_coinbases():
    _, _, pool = setup()
    with pytest.raises(ValidationError, match="nonce-gap"):
        pool.add(pay(ALICE, 1))
    with pytest.raises(ValidationError, match="not-a-transfer"):
        pool.add(Coinbase(PARAMS.network_id, 7, ALICE.address, 10 * TC))
    with pytest.raises(ValidationError, match="bad-signature"):
        pool.add(replace(pay(ALICE, 0), fee=999))


def test_waiting_transfers_cannot_overspend():
    """Alice has 20 TC: she can't have 15 + 6 TC waiting at the same time."""
    _, _, pool = setup()
    pool.add(pay(ALICE, 0, 15 * TC))
    with pytest.raises(ValidationError, match="insufficient-funds"):
        pool.add(pay(ALICE, 1, 6 * TC))
    pool.add(pay(ALICE, 1, 4 * TC))


def test_fee_policy():
    _, _, pool = setup()
    tx = pay(ALICE, 0, fee=145)  # 146 bytes at 1 base unit per byte needs 146
    with pytest.raises(ValidationError, match="fee-too-low"):
        pool.add(tx)
    _, _, free_pool = setup(min_fee_per_byte=0)
    free_pool.add(pay(ALICE, 0, fee=0))


def test_replace_by_fee():
    _, _, pool = setup()
    original = pay(ALICE, 0, fee=200)
    pool.add(original)
    with pytest.raises(ValidationError, match="replacement-fee-too-low"):
        pool.add(pay(ALICE, 0, to=CAROL, fee=240))
    better = pay(ALICE, 0, to=CAROL, fee=250)
    pool.add(better)
    assert original.txid not in pool and better.txid in pool and len(pool) == 1


def test_select_orders_by_fee_per_byte_and_keeps_nonce_order():
    _, _, pool = setup()
    alice0, alice1, bob0 = pay(ALICE, 0, fee=200), pay(ALICE, 1, fee=5000), pay(BOB, 0, to=CAROL, fee=1000)
    for tx in (alice0, alice1, bob0):
        pool.add(tx)
    assert pool.select(10_000) == [bob0, alice0, alice1]
    assert pool.select(2 * alice0.size) == [bob0, alice0]


def test_template_is_a_valid_block_and_pays_the_fees():
    _, node, pool = setup()
    txs = [pay(ALICE, 0, fee=300), pay(BOB, 0, to=CAROL, fee=700)]
    for tx in txs:
        pool.add(tx)
    block = mine_template(node, pool)
    assert block.coinbase.amount == PARAMS.block_reward + 1000
    result = node.submit_block(block)
    assert result.outcome is Outcome.NEW_TIP
    pool.update(result.connected, result.disconnected)
    assert len(pool) == 0
    assert node.get_account(CAROL.address).balance == TC
    # Alice's next transfer continues from her confirmed nonce.
    pool.add(pay(ALICE, 1))
    with pytest.raises(ValidationError, match="nonce-too-low"):
        pool.add(pay(ALICE, 0, to=CAROL))


def test_template_respects_the_block_size_limit():
    ref, node, _ = setup()
    small = replace(PARAMS, max_block_size=156 + 4 + 41 + 2 * 146)  # header, count, coinbase, two transfers
    pool = Mempool(small, node.store, clock=lambda: NOW)
    for nonce in range(4):
        pool.add(pay(ALICE, nonce))
    node.params = small
    template = build_template(node, pool, MINER.address, now=NOW)
    assert len(template.transactions) == 3 and template.size <= small.max_block_size


def test_reorg_puts_transfers_back_and_drops_conflicts():
    ref, node, pool = setup()
    to_bob = pay(ALICE, 0, 5 * TC)
    pool.add(to_bob)
    ours = mine_template(node, pool)
    result = node.submit_block(ours)
    pool.update(result.connected, result.disconnected)
    assert len(pool) == 0

    # A longer branch without our block: the transfer comes back to the mempool.
    other = copy.deepcopy(ref)
    other.mine_blocks(2)
    for block in other.blocks[7:]:
        result = node.submit_block(block)
        pool.update(result.connected, result.disconnected)
    assert node.tip.block_id == other.tip.block_id
    assert to_bob.txid in pool

    # An even longer branch where Alice's nonce 0 paid Carol instead: the transfer to Bob is dropped.
    rival = copy.deepcopy(ref)
    rival.add(rival.build_block([pay(ALICE, 0, 5 * TC, to=CAROL)]))
    rival.mine_blocks(2)
    for block in rival.blocks[7:]:
        result = node.submit_block(block)
        pool.update(result.connected, result.disconnected)
    assert node.tip.block_id == rival.tip.block_id
    assert len(pool) == 0


def test_full_mempool_evicts_the_lowest_fee():
    _, _, pool = setup(max_bytes=3 * 146)
    alice0, alice1, bob0 = pay(ALICE, 0, fee=200), pay(ALICE, 1, fee=1000), pay(BOB, 0, to=CAROL, fee=300)
    for tx in (alice0, alice1, bob0):
        pool.add(tx)
    pool.add(pay(ALICE, 2, fee=2000))  # over the limit: Bob's (the cheapest last-in-line transfer) leaves
    assert bob0.txid not in pool and len(pool) == 3
    with pytest.raises(ValidationError, match="mempool-full"):
        pool.add(pay(BOB, 0, to=CAROL, fee=150))


def test_old_transfers_expire():
    clock = Clock(NOW)
    _, _, pool = setup(clock=clock, expiry=3600)
    pool.add(pay(ALICE, 0))
    clock.now += 1800
    pool.add(pay(ALICE, 1))
    pool.add(pay(BOB, 0, to=CAROL))
    clock.now += 1801
    assert pool.expire() == 2  # Alice's first, and her second (which depends on it)
    assert len(pool) == 1 and pool.next_nonce(ALICE.address) == 0


class ScanningMempool(Mempool):
    """The obvious, slow way to choose what to evict: look at every sender each time."""

    def _trim_to_size(self) -> None:
        while self._bytes > self.max_bytes:
            last_of_each = (pending[max(pending)] for pending in self._by_sender.values())
            self._remove(min(last_of_each, key=lambda e: (e.fee_rate, -e.added, e.tx.txid)))


def outcome(pool: Mempool, tx) -> str:
    try:
        pool.add(tx)
        return "added"
    except ValidationError as error:
        return error.code


@pytest.mark.parametrize("seed", range(3))
def test_indexes_and_eviction_match_scanning_everything(seed):
    rng = random.Random(seed)
    senders = [named_key(f"sender-{i}") for i in range(6)]
    everyone = [*senders, CAROL, MINER]
    ref = TestChain(PARAMS)
    for key in senders:
        ref.mine_blocks(1, miner=key.address)  # 10 TC each
    ref.mine_blocks(3, miner=MINER.address)
    node = ChainManager(Store(":memory:"), PARAMS, clock=lambda: NOW)
    for block in ref.blocks[1:]:
        node.submit_block(block)
    clock = Clock(NOW)
    pool = Mempool(PARAMS, node.store, clock=clock, max_bytes=3000)
    scanning = ScanningMempool(PARAMS, node.store, clock=clock, max_bytes=3000)

    for step in range(300):
        clock.now += rng.choice([0, 0, 1])  # plenty of ties in time
        sender = rng.choice(senders)
        confirmed = node.get_account(sender.address).nonce
        nonce = pool.next_nonce(sender.address)
        if nonce > confirmed and rng.random() < 0.25:
            nonce = rng.randrange(confirmed, nonce)  # a replacement
        receivers = [(rng.choice(everyone).address, rng.randint(1, 10_000)) for _ in range(rng.randint(1, 3))]
        tx = make_transfer(PARAMS, sender, nonce, receivers, fee=rng.randint(100, 3000),
                           memo=bytes(rng.randrange(40)))
        assert outcome(pool, tx) == outcome(scanning, tx)
        if step % 60 == 59:  # a block with some of them
            block = mine_template(node, pool)
            result = node.submit_block(block)
            pool.update(result.connected, result.disconnected)
            scanning.update(result.connected, result.disconnected)

        assert {e.tx.txid for e in pool.entries()} == {e.tx.txid for e in scanning.entries()}
        assert pool.size_bytes == sum(e.size for e in pool.entries()) <= 3000
        for key in everyone:
            address = key.address
            scan_in = {e.tx.txid for e in pool.entries() if any(o.address == address for o in e.tx.outputs)}
            scan_all = scan_in | {e.tx.txid for e in pool.entries() if e.tx.sender == address}
            assert {e.tx.txid for e in pool.incoming_for(address)} == scan_in
            involving = pool.involving(address)
            assert {e.tx.txid for e in involving} == scan_all
            assert [e.added for e in involving] == sorted((e.added for e in involving), reverse=True)
