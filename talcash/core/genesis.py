"""The genesis block: block 0, hard-coded into every node.

Its reward goes to the burn address (an all-zero payload that no key can
produce), so nobody, including the creator, can ever spend it.
"""

from functools import cache

from ..crypto.address import BURN_PAYLOAD
from .block import BLOCK_VERSION, Block, BlockHeader, compute_merkle_root
from .params import NetworkParams
from .pow import mine
from .snapshot import NO_SNAPSHOT
from .tx import Coinbase


def build_genesis(params: NetworkParams, *, timestamp: int, message: bytes, nonce: int = 0) -> Block:
    coinbase = Coinbase(
        network_id=params.network_id,
        height=0,
        address=BURN_PAYLOAD,
        amount=params.block_reward,
        memo=message,
    )
    header = BlockHeader(
        version=BLOCK_VERSION,
        height=0,
        prev_id=bytes(32),
        merkle_root=compute_merkle_root([coinbase]),
        snapshot_root=NO_SNAPSHOT,
        timestamp=timestamp,
        target=params.genesis_target,
        nonce=nonce,
    )
    return Block(header, (coinbase,))


def mine_genesis(params: NetworkParams, *, timestamp: int, message: bytes) -> Block:
    block = build_genesis(params, timestamp=timestamp, message=message)
    header = mine(block.header, params.pow)
    assert header is not None
    return Block(header, block.transactions)


@cache
def genesis_block(params: NetworkParams) -> Block:
    if params.genesis_timestamp is None or params.genesis_nonce is None or params.genesis_id is None:
        raise RuntimeError(f"{params.name} has not been launched yet: it has no genesis block")
    block = build_genesis(
        params, timestamp=params.genesis_timestamp, message=params.genesis_message, nonce=params.genesis_nonce
    )
    if block.block_id != params.genesis_id:
        raise RuntimeError(f"{params.name} genesis parameters do not produce the expected genesis id")
    return block
