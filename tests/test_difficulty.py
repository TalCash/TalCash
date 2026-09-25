import pytest

from talcash.core.block import BlockHeader
from talcash.core.difficulty import asert_target, block_work, median_time_past, next_target
from talcash.core.genesis import genesis_block
from talcash.core.params import MAINNET, REGTEST

ANCHOR = 1 << 200
LIMIT = 1 << 250


def target_at(parent_timestamp, parent_height, anchor=ANCHOR, limit=LIMIT):
    return asert_target(
        anchor_target=anchor, anchor_timestamp=0, anchor_height=0,
        parent_timestamp=parent_timestamp, parent_height=parent_height,
        spacing=60, half_life=3600, pow_limit=limit,
    )


def test_on_schedule_keeps_the_target_exactly():
    for height in (0, 1, 100, 10_000):
        assert target_at(height * 60, height) == ANCHOR


def test_one_half_life_behind_or_ahead_is_exactly_double_or_half():
    assert target_at(100 * 60 + 3600, 100) == 2 * ANCHOR  # blocks too slow -> easier
    assert target_at(100 * 60 - 3600, 100) == ANCHOR // 2  # blocks too fast -> harder


def test_target_moves_smoothly_with_time():
    targets = [target_at(6000 + seconds, 100) for seconds in range(-600, 601, 60)]
    assert targets == sorted(targets)
    for a, b in zip(targets, targets[1:]):
        assert 1.0 < b / a < 1.02  # one minute moves the target by about 1.2%


def test_clamps_and_extreme_inputs():
    assert target_at(10**12, 1) == LIMIT
    assert target_at(-(10**12), 10**6) == 1
    assert target_at(2**63, 0) == LIMIT  # returns instantly instead of shifting by trillions of bits


def test_regtest_never_retargets():
    genesis = genesis_block(REGTEST).header
    parent = BlockHeader(1, 50, bytes(32), bytes(32), bytes(32), genesis.timestamp + 5, 0, 0)
    assert next_target(REGTEST, genesis, parent) == REGTEST.genesis_target


def simulate(hashrates, spacing=60, half_life=3600):
    """Deterministic simulation: each block takes exactly (expected hashes / hashrate) seconds."""
    genesis_target = MAINNET.genesis_target
    timestamp, times, targets = 0, [], []
    for height, hashrate in enumerate(hashrates):
        target = asert_target(
            anchor_target=genesis_target, anchor_timestamp=0, anchor_height=0,
            parent_timestamp=timestamp, parent_height=height,
            spacing=spacing, half_life=half_life, pow_limit=MAINNET.pow_limit,
        )
        block_time = max(1, round(block_work(target) / hashrate))
        timestamp += block_time
        times.append(block_time)
        targets.append(target)
    return times, targets


def test_ten_times_more_miners_is_absorbed_gradually():
    one_core = 300  # hashes per second; the genesis target gives this one block per minute
    times, targets = simulate([one_core] * 60 + [10 * one_core] * 1500)
    assert 58 <= times[59] <= 62
    # Not instant: ten blocks after the jump, difficulty has risen by less than 2x.
    assert targets[60] / targets[70] < 2
    # But it gets there: block times return to about a minute.
    recent = times[-200:]
    assert 55 <= sum(recent) / len(recent) <= 65


def test_miners_leaving_makes_it_easier_again():
    one_core = 300
    times, targets = simulate([10 * one_core] * 300 + [one_core] * 1500)
    assert targets[-1] > targets[299]
    recent = times[-200:]
    assert 55 <= sum(recent) / len(recent) <= 65


def test_block_work_and_median_time_past():
    assert block_work((1 << 256) - 1) == 1
    assert block_work((1 << 255) - 1) == 2
    assert median_time_past([5, 1, 3]) == 3
    assert median_time_past([1, 2, 3, 4]) == 3
