"""The node's chain manager, checked against the in-memory reference chain (TestChain).

Branches are built with TestChain; the node must end up with exactly the same
tip, balances and snapshots as the reference for whichever branch has most work.
"""

import copy
import random
from dataclasses import replace

import pytest

from technocoin.core.amounts import COIN
from technocoin.core.block import Block
from technocoin.core.genesis import mine_genesis
from technocoin.core.params import REGTEST
from technocoin.core.pow import meets_target
from technocoin.node.chain import ChainManager, Outcome
from technocoin.node.store import STATUS_INVALID, Store

from chainutil import TestChain, make_transfer, named_key

# Small numbers so short chains exercise everything: rewards mature after 3 blocks,
# snapshots every 5 blocks (after 4, 9, 14, ...), history final 10 blocks deep.
PARAMS = replace(REGTEST, coinbase_maturity=3, chunk_size=5, snapshot_delay=2, finality_depth=10)
NOW = 2_000_000_000  # a clock well after every test block

ALICE, BOB, CAROL = named_key("alice"), named_key("bob"), named_key("carol")
MINER_A, MINER_B = named_key("miner-a"), named_key("miner-b")


def new_node(path=":memory:", params=PARAMS, now=NOW) -> ChainManager:
    return ChainManager(Store(path), params, clock=lambda: now)


def reference(length: int, miner=ALICE) -> TestChain:
    chain = TestChain(PARAMS)
    chain.mine_blocks(length, miner=miner.address)
    return chain


def branch(chain: TestChain) -> TestChain:
    return copy.deepcopy(chain)


def feed(node: ChainManager, blocks) -> list:
    return [node.submit_block(block) for block in blocks]


def assert_matches(node: ChainManager, ref: TestChain) -> None:
    assert node.tip.block_id == ref.tip.block_id
    assert dict(node.store.all_accounts()) == ref.state.accounts
    for height in ref.snapshots:
        assert node.store.snapshot_root(height) == ref.snapshot_root_at(height)


def test_fresh_node_starts_at_genesis():
    node = new_node()
    assert node.tip_height == 0
    assert node.tip.block_id == node.genesis.block_id
    assert list(node.store.all_accounts()) == []


def test_follows_a_chain_with_payments():
    ref = reference(4)
    pay_bob = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 3 * COIN)], fee=100)
    ref.add(ref.build_block([pay_bob], miner=MINER_A.address))
    ref.mine_blocks(20, miner=MINER_A.address)

    node = new_node()
    results = feed(node, ref.blocks[1:])
    assert all(r.outcome is Outcome.NEW_TIP for r in results)
    assert_matches(node, ref)
    assert node.next_block_context() == ref.context()

    location = node.store.find_tx(pay_bob.txid)
    assert location.height == 5 and location.position == 1
    assert [row[2] for row in node.store.address_history(BOB.address)] == [pay_bob.txid]


def test_duplicates_and_bad_blocks():
    ref = reference(3)
    node = new_node()
    feed(node, ref.blocks[1:])
    assert node.submit_block(ref.blocks[2]).outcome is Outcome.DUPLICATE

    good = ref.build_block()
    nonce = 0
    while meets_target(replace(good.header, nonce=nonce), PARAMS.pow):
        nonce += 1
    bad_pow = Block(replace(good.header, nonce=nonce), good.transactions)
    result = node.submit_block(bad_pow)
    assert result.outcome is Outcome.INVALID and result.error.code == "bad-pow"
    assert node.store.header(bad_pow.block_id) is None  # junk is never stored

    assert node.submit_block(node.genesis).outcome is Outcome.DUPLICATE
    fake_genesis = Block(replace(node.genesis.header, timestamp=1), node.genesis.transactions)
    assert node.submit_block(fake_genesis).error.code == "bad-genesis"
    assert node.submit_block(good).outcome is Outcome.NEW_TIP


def test_blocks_from_the_future_are_refused():
    ref = reference(1)
    parent_time = ref.tip.header.timestamp
    node = new_node(now=parent_time + 1000)
    feed(node, ref.blocks[1:])
    too_new = ref.build_block(timestamp=parent_time + 1000 + PARAMS.max_future_drift + 1)
    assert node.submit_block(too_new).error.code == "time-too-new"
    just_ok = ref.build_block(timestamp=parent_time + 1000 + PARAMS.max_future_drift)
    assert node.submit_block(just_ok).outcome is Outcome.NEW_TIP


def test_orphans_wait_for_their_parent():
    ref = reference(6)
    node = new_node()
    for block in reversed(ref.blocks[2:]):
        assert node.submit_block(block).outcome is Outcome.ORPHAN
    result = node.submit_block(ref.blocks[1])
    assert result.outcome is Outcome.NEW_TIP
    assert [b.height for b in result.connected] == [1, 2, 3, 4, 5, 6]
    assert_matches(node, ref)


def test_switches_to_the_branch_with_more_work_and_back():
    base = reference(8)
    a, b = branch(base), branch(base)
    # Alice spends her first coins twice: to Bob on branch A, to Carol on branch B.
    pay_bob = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 5 * COIN)])
    pay_carol = make_transfer(PARAMS, ALICE, 0, [(CAROL.address, 7 * COIN)])
    a.add(a.build_block([pay_bob], miner=MINER_A.address))
    a.mine_blocks(3, miner=MINER_A.address)  # A reaches height 12
    b.add(b.build_block([pay_carol], miner=MINER_B.address))
    b.mine_blocks(5, miner=MINER_B.address)  # B reaches height 14

    node = new_node()
    feed(node, base.blocks[1:] + a.blocks[9:])
    assert_matches(node, a)

    results = feed(node, b.blocks[9:13])  # heights 9-12: never more work than A
    assert all(r.outcome is Outcome.SIDE_BRANCH for r in results)
    assert node.tip.block_id == a.tip.block_id

    switch = node.submit_block(b.blocks[13])
    assert switch.outcome is Outcome.NEW_TIP
    assert [blk.height for blk in switch.disconnected] == [12, 11, 10, 9]
    assert [blk.height for blk in switch.connected] == [9, 10, 11, 12, 13]
    feed(node, b.blocks[14:])
    assert_matches(node, b)
    assert node.get_account(BOB.address).balance == 0
    assert node.get_account(CAROL.address).balance == 7 * COIN
    assert node.store.find_tx(pay_bob.txid) is None
    assert node.store.find_tx(pay_carol.txid) is not None
    # The snapshot after block 9 was recomputed for branch B.
    assert node.store.snapshot_root(9) == b.snapshot_root_at(9) != a.snapshot_root_at(9)

    a.mine_blocks(4, miner=MINER_A.address)  # A overtakes: height 16
    feed(node, a.blocks[13:])
    assert_matches(node, a)
    assert node.get_account(BOB.address).balance == 5 * COIN
    assert node.get_account(CAROL.address).balance == 0


def test_equal_work_keeps_the_chain_seen_first():
    base = reference(8)
    a, b = branch(base), branch(base)
    a.mine_blocks(2, miner=MINER_A.address)
    b.mine_blocks(2, miner=MINER_B.address)
    node = new_node()
    feed(node, base.blocks[1:] + a.blocks[9:])
    assert [r.outcome for r in feed(node, b.blocks[9:])] == [Outcome.SIDE_BRANCH] * 2
    assert_matches(node, a)


def test_invalid_branch_is_rolled_back_and_remembered():
    base = reference(8)
    a, b = branch(base), branch(base)
    a.mine_blocks(2, miner=MINER_A.address)  # A: height 10
    b.mine_blocks(1, miner=MINER_B.address)  # B9 is fine
    # B10 has valid proof of work and signatures, but Bob spends money he doesn't have.
    b10 = b.build_block([make_transfer(PARAMS, BOB, 0, [(CAROL.address, COIN)])], miner=MINER_B.address)
    b.blocks.append(b10)  # force it onto the reference so we can mine on top of it
    b11 = b.build_block(miner=MINER_B.address)
    b.blocks.append(b11)
    b12 = b.build_block(miner=MINER_B.address)

    node = new_node()
    feed(node, base.blocks[1:] + a.blocks[9:])
    before = dict(node.store.all_accounts())

    assert node.submit_block(b.blocks[9]).outcome is Outcome.SIDE_BRANCH
    assert node.submit_block(b10).outcome is Outcome.SIDE_BRANCH  # equal work: stored, not yet checked
    result = node.submit_block(b11)  # more work: the node tries B and finds B10 invalid
    assert result.outcome is Outcome.INVALID and result.error.code == "insufficient-funds"

    assert_matches(node, a)  # still on A, balances untouched
    assert dict(node.store.all_accounts()) == before
    assert node.store.header(b10.block_id).status == STATUS_INVALID
    assert node.store.header(b11.block_id).status == STATUS_INVALID
    assert node.submit_block(b12).error.code == "invalid-parent"
    assert node.submit_block(b10).error.code == "known-invalid"


def test_history_below_the_finality_depth_cannot_change():
    prefix = reference(19)
    main = branch(prefix)
    main.mine_blocks(11)  # height 30: final up to height 20
    node = new_node()
    feed(node, main.blocks[1:])
    assert node.finalized_height() == 20

    splits_at_19 = branch(prefix)
    splits_at_19.mine_blocks(1, miner=MINER_B.address)
    assert node.submit_block(splits_at_19.blocks[20]).error.code == "fork-below-finality"

    splits_at_20 = branch(prefix)
    splits_at_20.mine_blocks(1)  # identical to main's block 20
    splits_at_20.mine_blocks(1, miner=MINER_B.address)
    assert splits_at_20.blocks[20].block_id == main.blocks[20].block_id
    assert node.submit_block(splits_at_20.blocks[21]).outcome is Outcome.SIDE_BRANCH


def test_side_branch_stored_earlier_cannot_grow_once_its_fork_is_final():
    prefix = reference(19)
    main, side = branch(prefix), branch(prefix)
    main.mine_blocks(11)  # height 30
    side.mine_blocks(3, miner=MINER_B.address)  # splits after block 19

    node = new_node()
    feed(node, main.blocks[1:26])  # tip 25: final up to 15, so a split at 19 is still allowed
    assert node.submit_block(side.blocks[20]).outcome is Outcome.SIDE_BRANCH
    feed(node, main.blocks[26:])  # tip 30: final up to 20
    # The side block's parent is itself a side block, at a height that is now final.
    assert node.submit_block(side.blocks[21]).error.code == "fork-below-finality"


@pytest.mark.parametrize("order", ["shuffled", "by-height"])
@pytest.mark.parametrize("seed", range(5))
def test_random_branches_end_on_the_heaviest(seed, order):
    """Shuffled: most blocks arrive before their parent. By height: like a live network,
    competing branches arrive interleaved, causing many side branches and switches."""
    rng = random.Random(seed)
    branches = [reference(4)]
    for _ in range(40):
        parent = rng.choice(branches)
        chain = branch(parent) if rng.random() < 0.3 else parent
        if chain is not parent:
            branches.append(chain)
        alice = chain.state.get_account(ALICE.address)
        transfers = []
        if alice.balance > COIN and rng.random() < 0.5:
            receiver = rng.choice([BOB, CAROL]).address
            transfers = [make_transfer(PARAMS, ALICE, alice.nonce, [(receiver, rng.randint(1, COIN))],
                                       fee=rng.randint(0, 50))]
        chain.add(chain.build_block(transfers, miner=rng.choice([MINER_A, MINER_B, ALICE]).address))
    # Make one branch strictly heaviest so the expected winner doesn't depend on arrival order.
    winner = rng.choice(branches)
    winner.mine_blocks(max(c.tip.height for c in branches) - winner.tip.height + 1, miner=MINER_A.address)

    blocks = list({block.block_id: block for chain in branches for block in chain.blocks[1:]}.values())
    rng.shuffle(blocks)
    if order == "by-height":
        blocks.sort(key=lambda block: block.height)  # stable: ties stay in random order
    node = new_node(params=replace(PARAMS, finality_depth=1000))
    feed(node, blocks)
    assert_matches(node, winner)


def test_restart_keeps_everything(tmp_path):
    ref = reference(4)
    ref.add(ref.build_block([make_transfer(PARAMS, ALICE, 0, [(BOB.address, COIN)])]))
    ref.mine_blocks(7)
    path = tmp_path / "chain.sqlite"

    node = new_node(path)
    feed(node, ref.blocks[1:-1])
    node.store.close()

    reopened = new_node(path)
    assert reopened.tip_height == ref.tip.height - 1
    assert reopened.submit_block(ref.blocks[-1]).outcome is Outcome.NEW_TIP
    assert_matches(reopened, ref)


def test_database_from_another_network_is_refused(tmp_path):
    path = tmp_path / "chain.sqlite"
    new_node(path).store.close()
    other_genesis = mine_genesis(PARAMS, timestamp=PARAMS.genesis_timestamp, message=b"another chain")
    other = replace(PARAMS, genesis_message=b"another chain",
                    genesis_nonce=other_genesis.header.nonce, genesis_id=other_genesis.block_id)
    with pytest.raises(RuntimeError, match="different network"):
        new_node(path, params=other)
