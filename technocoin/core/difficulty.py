"""Difficulty: ASERT (absolutely scheduled exponentially rising targets).

Adapted from Bitcoin Cash's aserti3-2d (2020). The chain has an ideal schedule,
genesis time + height * 60 s. Each block's target is set by how far the chain is
ahead of or behind that schedule:

    target = genesis_target * 2 ** ((behind_schedule_seconds) / half_life)

Ahead of schedule (blocks too fast) -> exponent negative -> smaller target -> harder.
Behind (blocks too slow) -> easier. With a one-hour half-life, being one hour
ahead doubles the difficulty. Difficulty moves a little on every block, never in
jumps, and the long-run average block time stays exactly on target.

All arithmetic is on integers (2**x is a fixed-point cubic approximation, max
error about 0.013%), so every implementation computes the same target.
Division is floor division (rounds toward negative infinity).
"""

from collections.abc import Sequence

from .block import BlockHeader
from .params import NetworkParams

_RADIX_BITS = 16
_RADIX = 1 << _RADIX_BITS


def asert_target(
    *,
    anchor_target: int,
    anchor_timestamp: int,
    anchor_height: int,
    parent_timestamp: int,
    parent_height: int,
    spacing: int,
    half_life: int,
    pow_limit: int,
) -> int:
    """Target for the block that extends `parent`."""
    time_delta = parent_timestamp - anchor_timestamp
    height_delta = parent_height - anchor_height
    exponent = ((time_delta - spacing * height_delta) * _RADIX) // half_life

    shifts = exponent >> _RADIX_BITS
    frac = exponent - (shifts << _RADIX_BITS)  # 0 <= frac < 65536
    # Absurd exponents (chain stalled for years, or years ahead) would only clamp
    # anyway; stop before shifting by millions of bits.
    if shifts > 256:
        return pow_limit
    if shifts < -512:
        return 1

    factor = _RADIX + (
        (195_766_423_245_049 * frac + 971_821_376 * frac**2 + 5_127 * frac**3 + (1 << 47)) >> 48
    )
    target = anchor_target * factor
    target = target << shifts if shifts >= 0 else target >> -shifts
    target >>= _RADIX_BITS
    return max(1, min(target, pow_limit))


def next_target(params: NetworkParams, genesis: BlockHeader, parent: BlockHeader) -> int:
    if params.pow_no_retarget:
        return params.genesis_target
    return asert_target(
        anchor_target=genesis.target,
        anchor_timestamp=genesis.timestamp,
        anchor_height=genesis.height,
        parent_timestamp=parent.timestamp,
        parent_height=parent.height,
        spacing=params.target_spacing,
        half_life=params.asert_half_life,
        pow_limit=params.pow_limit,
    )


def block_work(target: int) -> int:
    """Expected number of hashes needed to find a block at this target."""
    return (1 << 256) // (target + 1)


def median_time_past(timestamps: Sequence[int]) -> int:
    """Median of the given timestamps (the node passes the last 11 blocks' times)."""
    ordered = sorted(timestamps)
    return ordered[len(ordered) // 2]


def difficulty(target: int, params: NetworkParams) -> float:
    """Human-friendly difficulty: 1.0 at launch, 2.0 means twice as hard."""
    return params.genesis_target / target
