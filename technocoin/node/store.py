"""SQLite storage for one node.

Blocks themselves live in block files (blockfiles.py); the database only
records where each one is. Everything here can be rebuilt from those files.

Tables
  headers        every block the node has stored, on any branch: header, total chain work,
                 and where its bytes are (a recent block file, or a position in a chunk file)
  main_chain     height -> block id, for the active chain
  accounts       balances and nonces at the tip of the active chain
  undo           for each block on the active chain: the account values before it
  snapshots      snapshot roots for snapshot points on the active chain
  tx_index       where each transaction of the active chain is
  address_index  which transactions of the active chain touch each address (wallet history)
  meta           network name, genesis id, schema version

The store only reads and writes; ChainManager decides what to write. Every
change for one operation happens inside one transaction(), so a crash or a
failed validation never leaves half-applied state behind.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..core.block import Block, BlockHeader, block_from_bytes, header_from_bytes
from ..core.state import EMPTY_ACCOUNT, Account
from ..core.tx import Coinbase, Transfer
from .blockfiles import BlockFiles, ChunkLocation

SCHEMA_VERSION = 2

STATUS_STORED = 0  # checked on arrival, not yet connected to the active chain
STATUS_VALID = 1  # has been fully validated (connected at least once)
STATUS_INVALID = 2  # breaks a rule, or descends from a block that does

_WORK_BYTES = 48  # big-endian, so SQLite's byte comparison orders by work

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS headers (
    block_id   BLOB PRIMARY KEY,
    prev_id    BLOB NOT NULL,
    height     INTEGER NOT NULL,
    header     BLOB NOT NULL,
    chain_work BLOB NOT NULL,
    status     INTEGER NOT NULL,
    chunk      INTEGER,  -- NULL: the block is in its own recent file
    segment_offset INTEGER,
    segment_length INTEGER,
    position   INTEGER
);
CREATE INDEX IF NOT EXISTS headers_by_prev ON headers(prev_id);
CREATE INDEX IF NOT EXISTS headers_by_height ON headers(height);
CREATE TABLE IF NOT EXISTS main_chain (height INTEGER PRIMARY KEY, block_id BLOB NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS accounts (address BLOB PRIMARY KEY, balance INTEGER NOT NULL, nonce INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS undo (block_id BLOB PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (height INTEGER PRIMARY KEY, root BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS tx_index (
    txid BLOB PRIMARY KEY, block_id BLOB NOT NULL, height INTEGER NOT NULL, position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS address_index (
    address BLOB NOT NULL, height INTEGER NOT NULL, position INTEGER NOT NULL, txid BLOB NOT NULL,
    PRIMARY KEY (address, height, position)
);
"""


@dataclass(frozen=True)
class StoredHeader:
    header: BlockHeader
    chain_work: int
    status: int

    @property
    def block_id(self) -> bytes:
        return self.header.block_id

    @property
    def height(self) -> int:
        return self.header.height


@dataclass(frozen=True)
class TxLocation:
    block_id: bytes
    height: int
    position: int


class Store:
    def __init__(self, path: Path | str, blocks_dir: Path | None = None) -> None:
        """`blocks_dir` defaults to a `blocks` folder next to the database (kept in memory for ":memory:")."""
        self.path = str(path)
        if self.path == ":memory:":
            self.files = BlockFiles(blocks_dir)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.files = BlockFiles(blocks_dir or Path(self.path).parent / "blocks")
        self._db = sqlite3.connect(self.path, isolation_level=None)
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._in_transaction = False
        self._db.executescript(_SCHEMA)
        version = self.meta("schema_version")
        if version is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif version != str(SCHEMA_VERSION):
            raise RuntimeError(f"{self.path} is from an older version; rebuild it with `tc node --reindex`")

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All-or-nothing: if anything inside raises, every change is rolled back."""
        if self._in_transaction:
            raise RuntimeError("transactions don't nest")
        self._db.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")
        finally:
            self._in_transaction = False

    def _one(self, sql: str, args: tuple = ()) -> tuple | None:
        return self._db.execute(sql, args).fetchone()

    # --- meta ---------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # --- headers and blocks -------------------------------------------------

    def add_block(self, block: Block, chain_work: int, status: int, location: ChunkLocation | None = None) -> None:
        """Record a block. Without `location`, its bytes go into a new recent block file."""
        header = block.header
        if location is None:
            self.files.write_recent(block)
            where = (None, None, None, None)
        else:
            where = (location.chunk, location.segment_offset, location.segment_length, location.position)
        self._db.execute(
            "INSERT INTO headers (block_id, prev_id, height, header, chain_work, status,"
            " chunk, segment_offset, segment_length, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (header.block_id, header.prev_id, header.height, header.serialize(),
             chain_work.to_bytes(_WORK_BYTES, "big"), status, *where),
        )

    def set_location(self, block_id: bytes, location: ChunkLocation) -> None:
        self._db.execute(
            "UPDATE headers SET chunk = ?, segment_offset = ?, segment_length = ?, position = ? WHERE block_id = ?",
            (location.chunk, location.segment_offset, location.segment_length, location.position, block_id),
        )

    def header(self, block_id: bytes) -> StoredHeader | None:
        row = self._one("SELECT header, chain_work, status FROM headers WHERE block_id = ?", (block_id,))
        if row is None:
            return None
        return StoredHeader(header_from_bytes(row[0]), int.from_bytes(row[1], "big"), row[2])

    def block_bytes(self, block_id: bytes) -> bytes | None:
        row = self._one(
            "SELECT height, chunk, segment_offset, segment_length, position FROM headers WHERE block_id = ?",
            (block_id,),
        )
        if row is None:
            return None
        height, chunk, offset, length, position = row
        if chunk is None:
            return self.files.read_recent(height, block_id)
        return self.files.read_from_chunk(ChunkLocation(chunk, offset, length, position))

    def block(self, block_id: bytes) -> Block | None:
        data = self.block_bytes(block_id)
        return block_from_bytes(data) if data is not None else None

    # --- sealing chunks ---------------------------------------------------------

    def sealed_chunks(self) -> int:
        return int(self.meta("sealed_chunks") or 0)

    def seal_chunk(self, network_id: int, index: int, first_height: int, last_height: int) -> None:
        """Write one chunk file of the active chain's blocks first..last, then point the index at it.

        Safe to repeat after a crash at any point: the chunk file is rewritten identically, and recent
        files are only deleted once the database says their blocks are in the chunk.
        """
        ids = [self.main_id(height) for height in range(first_height, last_height + 1)]
        locations = self.files.write_chunk(network_id, index, first_height, [self.block_bytes(i) for i in ids])
        losers = self._db.execute(
            "SELECT height, block_id FROM headers WHERE height BETWEEN ? AND ?"
            " AND block_id NOT IN (SELECT block_id FROM main_chain)",
            (first_height, last_height),
        ).fetchall()
        with self.transaction():
            for block_id, location in zip(ids, locations):
                self.set_location(block_id, location)
            for _, block_id in losers:  # branches that lost; below finality they can never win
                self._db.execute("DELETE FROM headers WHERE block_id = ?", (block_id,))
            self.set_meta("sealed_chunks", str(index + 1))
        for height, block_id in [*zip(range(first_height, last_height + 1), ids), *losers]:
            self.files.delete_recent(height, block_id)

    def tidy_recent_files(self, last_sealed_height: int) -> None:
        """Remove recent files a crash left behind while sealing."""
        for height, block_id in self.files.recent_blocks():
            if height > last_sealed_height:
                break
            row = self._one("SELECT chunk FROM headers WHERE block_id = ?", (block_id,))
            if row is None or row[0] is not None:
                self.files.delete_recent(height, block_id)

    def set_status(self, block_id: bytes, status: int) -> None:
        self._db.execute("UPDATE headers SET status = ? WHERE block_id = ?", (status, block_id))

    def children(self, block_id: bytes) -> list[bytes]:
        return [row[0] for row in self._db.execute("SELECT block_id FROM headers WHERE prev_id = ?", (block_id,))]

    # --- active chain -------------------------------------------------------

    def tip_height(self) -> int:
        return self._one("SELECT MAX(height) FROM main_chain")[0]

    def main_id(self, height: int) -> bytes | None:
        row = self._one("SELECT block_id FROM main_chain WHERE height = ?", (height,))
        return row[0] if row else None

    def main_headers(self, first: int, last: int) -> list[BlockHeader]:
        """Headers of the active chain from height `first` to `last`, in order."""
        rows = self._db.execute(
            "SELECT headers.header FROM main_chain JOIN headers ON headers.block_id = main_chain.block_id "
            "WHERE main_chain.height BETWEEN ? AND ? ORDER BY main_chain.height",
            (first, last),
        )
        return [header_from_bytes(row[0]) for row in rows]

    def set_main(self, height: int, block_id: bytes) -> None:
        self._db.execute("INSERT INTO main_chain (height, block_id) VALUES (?, ?)", (height, block_id))

    def delete_main(self, height: int) -> None:
        self._db.execute("DELETE FROM main_chain WHERE height = ?", (height,))

    # --- accounts (this is also the StateView that apply_block reads) --------

    def get_account(self, address: bytes) -> Account:
        row = self._one("SELECT balance, nonce FROM accounts WHERE address = ?", (address,))
        return Account(row[0], row[1]) if row else EMPTY_ACCOUNT

    def write_accounts(self, accounts: dict[bytes, Account]) -> None:
        for address, account in accounts.items():
            if account == EMPTY_ACCOUNT:
                self._db.execute("DELETE FROM accounts WHERE address = ?", (address,))
            else:
                self._db.execute(
                    "INSERT OR REPLACE INTO accounts (address, balance, nonce) VALUES (?, ?, ?)",
                    (address, account.balance, account.nonce),
                )

    def all_accounts(self) -> Iterator[tuple[bytes, Account]]:
        for address, balance, nonce in self._db.execute("SELECT address, balance, nonce FROM accounts"):
            yield address, Account(balance, nonce)

    # --- undo data ----------------------------------------------------------

    def put_undo(self, block_id: bytes, previous: dict[bytes, Account]) -> None:
        data = b"".join(
            address + account.balance.to_bytes(8, "big") + account.nonce.to_bytes(8, "big")
            for address, account in previous.items()
        )
        self._db.execute("INSERT INTO undo (block_id, data) VALUES (?, ?)", (block_id, data))

    def take_undo(self, block_id: bytes) -> dict[bytes, Account]:
        row = self._one("SELECT data FROM undo WHERE block_id = ?", (block_id,))
        if row is None:
            raise RuntimeError("missing undo data for a block on the active chain")
        self._db.execute("DELETE FROM undo WHERE block_id = ?", (block_id,))
        data, previous = row[0], {}
        for offset in range(0, len(data), 37):
            entry = data[offset:offset + 37]
            previous[entry[:21]] = Account(int.from_bytes(entry[21:29], "big"), int.from_bytes(entry[29:37], "big"))
        return previous

    # --- snapshots ----------------------------------------------------------

    def snapshot_root(self, height: int) -> bytes | None:
        row = self._one("SELECT root FROM snapshots WHERE height = ?", (height,))
        return row[0] if row else None

    def put_snapshot(self, height: int, root: bytes) -> None:
        self._db.execute("INSERT INTO snapshots (height, root) VALUES (?, ?)", (height, root))

    def delete_snapshot(self, height: int) -> None:
        self._db.execute("DELETE FROM snapshots WHERE height = ?", (height,))

    # --- transaction and address indexes -------------------------------------

    def index_block(self, block: Block) -> None:
        for position, tx in enumerate(block.transactions):
            self._db.execute(
                "INSERT INTO tx_index (txid, block_id, height, position) VALUES (?, ?, ?, ?)",
                (tx.txid, block.block_id, block.height, position),
            )
            for address in _addresses_of(tx):
                self._db.execute(
                    "INSERT INTO address_index (address, height, position, txid) VALUES (?, ?, ?, ?)",
                    (address, block.height, position, tx.txid),
                )

    def unindex_block(self, block: Block) -> None:
        self._db.execute("DELETE FROM tx_index WHERE height = ?", (block.height,))
        self._db.execute("DELETE FROM address_index WHERE height = ?", (block.height,))

    def find_tx(self, txid: bytes) -> TxLocation | None:
        row = self._one("SELECT block_id, height, position FROM tx_index WHERE txid = ?", (txid,))
        return TxLocation(*row) if row else None

    def coinbase_heights(self, address: bytes, above: int) -> list[int]:
        """Heights above `above` whose coinbase (always position 0) pays `address`."""
        return [row[0] for row in self._db.execute(
            "SELECT height FROM address_index WHERE address = ? AND position = 0 AND height > ?", (address, above)
        )]

    def address_history(self, address: bytes, limit: int = 100) -> list[tuple[int, int, bytes]]:
        """(height, position, txid), newest first."""
        return self._db.execute(
            "SELECT height, position, txid FROM address_index WHERE address = ? "
            "ORDER BY height DESC, position DESC LIMIT ?",
            (address, limit),
        ).fetchall()


def _addresses_of(tx: Transfer | Coinbase) -> set[bytes]:
    if isinstance(tx, Coinbase):
        return {tx.address}
    return {tx.sender, *(output.address for output in tx.outputs)}
