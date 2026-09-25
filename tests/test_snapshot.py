from dataclasses import replace

import pytest

from talcash.core.errors import ValidationError
from talcash.core.hashing import sha256
from talcash.core.params import MAINNET, REGTEST
from talcash.core.snapshot import (
    EMPTY_STATE_ROOT,
    NO_SNAPSHOT,
    is_snapshot_point,
    snapshot_height,
    state_root,
)
from talcash.core.state import Account, MemoryState, apply_block

from chainutil import TestChain, make_transfer, named_key

# Tiny chunks (5 blocks, 2-block delay) so a short chain covers several snapshots:
# snapshots are taken after blocks 4, 9, 14, ... and first required at height 7.
PARAMS = replace(REGTEST, chunk_size=5, snapshot_delay=2, coinbase_maturity=3)
ALICE, BOB = named_key("alice"), named_key("bob")


@pytest.mark.parametrize(
    "height, expected",
    [(0, None), (1449, None), (1450, 1439), (2889, 1439), (2890, 2879), (1440 * 365 + 10, 1440 * 365 - 1)],
)
def test_mainnet_snapshot_schedule(height, expected):
    assert snapshot_height(height, MAINNET) == expected


def test_snapshot_points():
    assert is_snapshot_point(1439, MAINNET)
    assert not is_snapshot_point(1440, MAINNET)
    assert not is_snapshot_point(0, MAINNET)


def test_params_reject_a_delay_longer_than_a_chunk():
    with pytest.raises(ValueError):
        replace(REGTEST, chunk_size=5, snapshot_delay=5)


def test_state_root():
    a, b = ALICE.address, BOB.address
    accounts = {a: Account(5, 1), b: Account(7, 0)}
    root = state_root(accounts.items())

    assert state_root([]) == EMPTY_STATE_ROOT == sha256(b"")
    assert EMPTY_STATE_ROOT != NO_SNAPSHOT
    assert state_root(reversed(list(accounts.items()))) == root  # order doesn't matter
    assert state_root([*accounts.items(), (bytes(21), Account())]) == root  # empty accounts don't count
    assert state_root({a: Account(5, 2), b: Account(7, 0)}.items()) != root  # nonces are included
    assert state_root({a: Account(6, 1), b: Account(7, 0)}.items()) != root
    assert state_root({a: Account(5, 1)}.items()) != root
    with pytest.raises(ValueError):
        state_root([(a, Account(1, 0)), (a, Account(2, 0))])


def active_chain(blocks: int) -> TestChain:
    """Alice mines the first blocks, then pays Bob now and then."""
    chain = TestChain(PARAMS)
    chain.mine_blocks(4, miner=ALICE.address)
    nonce = 0
    while chain.tip.height < blocks:
        if chain.tip.height % 3 == 0:
            tx = make_transfer(PARAMS, ALICE, nonce, [(BOB.address, 1000 + nonce)], fee=nonce)
            chain.add(chain.build_block([tx]))
            nonce += 1
        else:
            chain.mine_blocks(1)
    return chain


def test_headers_carry_the_right_snapshot():
    chain = active_chain(22)
    assert sorted(chain.snapshots) == [4, 9, 14, 19]
    for block in chain.blocks:
        snap = snapshot_height(block.height, PARAMS)
        expected = NO_SNAPSHOT if snap is None else chain.snapshot_root_at(snap)
        assert block.header.snapshot_root == expected
    assert min(b.height for b in chain.blocks if b.header.snapshot_root != NO_SNAPSHOT) == 7
    # The snapshots really differ from each other as balances change.
    assert len({chain.snapshot_root_at(h) for h in chain.snapshots}) == 4


def test_wrong_snapshot_root_is_rejected():
    chain = active_chain(6)  # the next block (7) must carry the snapshot taken after block 4
    for wrong in (NO_SNAPSHOT, EMPTY_STATE_ROOT, b"\x01" * 32, chain.snapshot_root_at(4)[::-1]):
        with pytest.raises(ValidationError, match="bad-snapshot-root"):
            chain.add(chain.build_block(snapshot_root=wrong))
    chain.add(chain.build_block())

    early = TestChain(PARAMS)  # before the first snapshot, headers must say NO_SNAPSHOT
    with pytest.raises(ValidationError, match="bad-snapshot-root"):
        early.add(early.build_block(snapshot_root=EMPTY_STATE_ROOT))


def test_new_node_can_start_from_a_snapshot():
    """Fast sync: balances at a snapshot + the blocks after it = replaying from genesis."""
    chain = active_chain(30)
    start = 9

    # 1. The new node downloads the balances at snapshot 9 and checks them against
    #    the root that later headers commit to (blocks 12-16 carry it).
    downloaded = dict(chain.snapshots[start])
    assert state_root(downloaded.items()) == chain.blocks[12].header.snapshot_root

    # 2. It replays only blocks 10 onward. (It uses the chain's headers, plus the
    #    coinbases of the few blocks before the snapshot, which mature after it.)
    synced = MemoryState()
    synced.accounts = downloaded
    for block in chain.blocks[start + 1:]:
        synced.apply(apply_block(block, chain.context_at(block.height), synced, PARAMS))
        if is_snapshot_point(block.height, PARAMS):
            # From here on it computes the same snapshots as everyone else.
            assert state_root(synced.accounts.items()) == chain.snapshot_root_at(block.height)

    assert synced.accounts == chain.state.accounts
