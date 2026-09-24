"""SQLite storage for one node.

Tables
  headers        every block the node has stored, on any branch, with its total chain work
  block_data     the full bytes of those blocks
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

SCHEMA_VERSION = 1

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
    status     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS headers_by_prev ON headers(prev_id);
CREATE TABLE IF NOT EXISTS block_data (block_id BLOB PRIMARY KEY, data BLOB NOT NULL);
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
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._in_transaction = False
        if self.meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif self.meta("schema_version") != str(SCHEMA_VERSION):
            raise RuntimeError(f"{self.path} uses an unsupported database schema")

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

    def add_block(self, block: Block, chain_work: int, status: int) -> None:
        header = block.header
        self._db.execute(
            "INSERT INTO headers (block_id, prev_id, height, header, chain_work, status) VALUES (?, ?, ?, ?, ?, ?)",
            (header.block_id, header.prev_id, header.height, header.serialize(),
             chain_work.to_bytes(_WORK_BYTES, "big"), status),
        )
        self._db.execute("INSERT INTO block_data (block_id, data) VALUES (?, ?)", (header.block_id, block.serialize()))

    def header(self, block_id: bytes) -> StoredHeader | None:
        row = self._one("SELECT header, chain_work, status FROM headers WHERE block_id = ?", (block_id,))
        if row is None:
            return None
        return StoredHeader(header_from_bytes(row[0]), int.from_bytes(row[1], "big"), row[2])

    def block(self, block_id: bytes) -> Block | None:
        row = self._one("SELECT data FROM block_data WHERE block_id = ?", (block_id,))
        return block_from_bytes(row[0]) if row else None

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
