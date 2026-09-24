"""Block templates: the unmined block a miner works on."""

from ..core.block import BLOCK_VERSION, HEADER_SIZE, Block, BlockHeader, compute_merkle_root
from ..core.tx import Coinbase
from .chain import ChainManager
from .mempool import Mempool


def build_template(chain: ChainManager, mempool: Mempool, miner: bytes, *, now: int, memo: bytes = b"") -> Block:
    """A block extending the current tip, paying `miner` (address payload), with nonce 0."""
    params = chain.params
    ctx = chain.next_block_context()
    height = ctx.parent.height + 1

    placeholder = Coinbase(params.network_id, height, miner, 0, memo)  # the amount doesn't change the size
    room = params.max_block_size - HEADER_SIZE - 4 - placeholder.size
    transfers = mempool.select(room)
    coinbase = Coinbase(params.network_id, height, miner, params.block_reward + sum(tx.fee for tx in transfers), memo)
    transactions = (coinbase, *transfers)

    header = BlockHeader(
        version=BLOCK_VERSION,
        height=height,
        prev_id=ctx.parent.block_id,
        merkle_root=compute_merkle_root(transactions),
        snapshot_root=ctx.snapshot_root,
        timestamp=max(now, ctx.median_time_past + 1),
        target=ctx.expected_target,
        nonce=0,
    )
    return Block(header, transactions)
