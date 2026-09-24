"""Blocks.

Header layout (156 bytes, big-endian):
    version u32 | height u64 | prev_id 32 | merkle_root 32 | snapshot_root 32
    | timestamp u64 | target u256 | nonce u64

block_id      = SHA256(header)          -- the block's name, used to link blocks
proof hash    = Argon2id(header)        -- must be <= target (see pow.py)
merkle_root   = RFC 6962 tree over the txids, coinbase first
snapshot_root = fingerprint of all balances at the latest daily snapshot (see snapshot.py)

Block layout: header | tx_count u32 | transactions
"""

from dataclasses import dataclass
from functools import cached_property

from .encoding import Reader, Writer
from .errors import ValidationError
from .hashing import merkle_root, sha256
from .params import NetworkParams
from .tx import Coinbase, Transaction, check_transaction, read_transaction

BLOCK_VERSION = 1
HEADER_SIZE = 156


@dataclass(frozen=True)
class BlockHeader:
    version: int
    height: int
    prev_id: bytes
    merkle_root: bytes
    snapshot_root: bytes
    timestamp: int
    target: int
    nonce: int

    def serialize(self) -> bytes:
        w = Writer()
        w.u32(self.version).u64(self.height).fixed(self.prev_id, 32).fixed(self.merkle_root, 32)
        w.fixed(self.snapshot_root, 32).u64(self.timestamp).u256(self.target).u64(self.nonce)
        return w.getvalue()

    @cached_property
    def block_id(self) -> bytes:
        return sha256(self.serialize())


def read_header(r: Reader) -> BlockHeader:
    return BlockHeader(
        version=r.u32(),
        height=r.u64(),
        prev_id=r.fixed(32),
        merkle_root=r.fixed(32),
        snapshot_root=r.fixed(32),
        timestamp=r.u64(),
        target=r.u256(),
        nonce=r.u64(),
    )


def header_from_bytes(data: bytes) -> BlockHeader:
    r = Reader(data)
    header = read_header(r)
    r.expect_end()
    return header


def compute_merkle_root(transactions: tuple[Transaction, ...] | list[Transaction]) -> bytes:
    return merkle_root([tx.txid for tx in transactions])


@dataclass(frozen=True)
class Block:
    header: BlockHeader
    transactions: tuple[Transaction, ...]

    def serialize(self) -> bytes:
        w = Writer()
        w.raw(self.header.serialize()).u32(len(self.transactions))
        for tx in self.transactions:
            w.raw(tx.serialize())
        return w.getvalue()

    @cached_property
    def size(self) -> int:
        return len(self.serialize())

    @property
    def block_id(self) -> bytes:
        return self.header.block_id

    @property
    def height(self) -> int:
        return self.header.height

    @property
    def coinbase(self) -> Coinbase:
        coinbase = self.transactions[0]
        assert isinstance(coinbase, Coinbase)
        return coinbase


def read_block(r: Reader) -> Block:
    header = read_header(r)
    count = r.u32()
    return Block(header, tuple(read_transaction(r) for _ in range(count)))


def block_from_bytes(data: bytes) -> Block:
    r = Reader(data)
    block = read_block(r)
    r.expect_end()
    return block


def check_block(block: Block, params: NetworkParams) -> None:
    """Rules that need no chain state (includes checking every signature). Raises ValidationError."""
    if block.header.version != BLOCK_VERSION:
        raise ValidationError("bad-version", f"block version {block.header.version}")
    if block.size > params.max_block_size:
        raise ValidationError("block-too-large", f"{block.size} bytes")
    if not block.transactions or not isinstance(block.transactions[0], Coinbase):
        raise ValidationError("missing-coinbase")
    if any(isinstance(tx, Coinbase) for tx in block.transactions[1:]):
        raise ValidationError("extra-coinbase")
    if block.coinbase.height != block.header.height:
        raise ValidationError("bad-coinbase-height")
    if len({tx.txid for tx in block.transactions}) != len(block.transactions):
        raise ValidationError("duplicate-transaction")
    if compute_merkle_root(block.transactions) != block.header.merkle_root:
        raise ValidationError("bad-merkle-root")
    for tx in block.transactions:
        check_transaction(tx, params)
