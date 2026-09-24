"""A tiny single-branch chain for exercising the consensus rules without a node."""

import hashlib
from dataclasses import dataclass

from technocoin.core.block import BLOCK_VERSION, Block, BlockHeader, compute_merkle_root
from technocoin.core.difficulty import median_time_past, next_target
from technocoin.core.genesis import genesis_block
from technocoin.core.params import NetworkParams
from technocoin.core.pow import mine
from technocoin.core.snapshot import NO_SNAPSHOT, is_snapshot_point, snapshot_height, state_root
from technocoin.core.state import BlockContext, MemoryState, StateChanges, apply_block
from technocoin.core.tx import Coinbase, Output, Transfer
from technocoin.crypto import keys
from technocoin.crypto.address import payload_from_public_key


@dataclass(frozen=True)
class NamedKey:
    private_key: bytes

    @property
    def public_key(self) -> bytes:
        return keys.public_key(self.private_key)

    @property
    def address(self) -> bytes:
        return payload_from_public_key(self.public_key)


def named_key(name: str) -> NamedKey:
    return NamedKey(hashlib.sha256(name.encode()).digest())


def make_transfer(
    params: NetworkParams,
    sender: NamedKey,
    nonce: int,
    outputs: list[tuple[bytes, int]],
    fee: int = 0,
    memo: bytes = b"",
) -> Transfer:
    unsigned = Transfer(
        network_id=params.network_id,
        sender_public_key=sender.public_key,
        nonce=nonce,
        fee=fee,
        outputs=tuple(Output(address, amount) for address, amount in outputs),
        memo=memo,
    )
    return unsigned.sign(sender.private_key)


class TestChain:
    __test__ = False  # not a pytest test class

    def __init__(self, params: NetworkParams) -> None:
        self.params = params
        self.genesis = genesis_block(params)
        self.blocks: list[Block] = [self.genesis]
        self.state = MemoryState()
        self.history: list[StateChanges] = []
        # height -> accounts right after that block, for every snapshot point
        self.snapshots: dict[int, dict] = {}

    @property
    def tip(self) -> Block:
        return self.blocks[-1]

    def snapshot_root_at(self, height: int) -> bytes:
        return state_root(self.snapshots[height].items())

    def context_at(self, height: int) -> BlockContext:
        """Context for a block at `height`, built on this chain's blocks below it."""
        parent = self.blocks[height - 1].header
        recent = [block.header.timestamp for block in self.blocks[max(0, height - self.params.median_time_span):height]]
        due = height - self.params.coinbase_maturity
        snap = snapshot_height(height, self.params)
        return BlockContext(
            parent=parent,
            median_time_past=median_time_past(recent),
            expected_target=next_target(self.params, self.genesis.header, parent),
            matured_coinbase=self.blocks[due].coinbase if due >= 0 else None,
            snapshot_root=self.snapshot_root_at(snap) if snap is not None else NO_SNAPSHOT,
        )

    def context(self) -> BlockContext:
        return self.context_at(self.tip.height + 1)

    def build_block(
        self,
        transfers: tuple[Transfer, ...] | list[Transfer] = (),
        miner: bytes | None = None,
        *,
        timestamp: int | None = None,
        coinbase_amount: int | None = None,
        target: int | None = None,
        snapshot_root: bytes | None = None,
    ) -> Block:
        ctx = self.context()
        height = ctx.parent.height + 1
        fees = sum(tx.fee for tx in transfers)
        coinbase = Coinbase(
            network_id=self.params.network_id,
            height=height,
            address=miner if miner is not None else named_key("miner").address,
            amount=coinbase_amount if coinbase_amount is not None else self.params.block_reward + fees,
        )
        transactions = (coinbase, *transfers)
        header = BlockHeader(
            version=BLOCK_VERSION,
            height=height,
            prev_id=ctx.parent.block_id,
            merkle_root=compute_merkle_root(transactions),
            snapshot_root=snapshot_root if snapshot_root is not None else ctx.snapshot_root,
            timestamp=timestamp if timestamp is not None else ctx.parent.timestamp + self.params.target_spacing,
            target=target if target is not None else ctx.expected_target,
            nonce=0,
        )
        mined = mine(header, self.params.pow)
        assert mined is not None
        return Block(mined, transactions)

    def add(self, block: Block) -> StateChanges:
        changes = apply_block(block, self.context(), self.state, self.params)
        self.state.apply(changes)
        self.blocks.append(block)
        self.history.append(changes)
        if is_snapshot_point(block.height, self.params):
            self.snapshots[block.height] = dict(self.state.accounts)
        return changes

    def mine_blocks(self, count: int, miner: bytes | None = None) -> None:
        for _ in range(count):
            self.add(self.build_block(miner=miner))

    def balance(self, address: bytes) -> int:
        return self.state.get_account(address).balance
