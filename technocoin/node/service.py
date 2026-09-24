"""A node's core: chain, mempool and live events.

The API, the built-in miner and (next) peers all go through this one object,
so every new block or transfer, wherever it comes from, is validated the same
way, updates the mempool, and is announced to subscribers.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.amounts import format_amount
from ..core.block import Block
from ..core.difficulty import difficulty
from ..core.params import NetworkParams
from ..core.tx import Coinbase, Transaction, Transfer
from ..crypto.address import encode_address
from ..paths import network_dir
from .chain import ChainManager, SubmitResult
from .events import EventBus
from .mempool import Mempool, MempoolEntry
from .network import CHAIN_FILE
from .store import Store, TxLocation
from .template import build_template
from .views import amount


def print_now(line: str) -> None:
    """print() that shows up immediately even when output goes to a file or a service log."""
    print(line, flush=True)


@dataclass(frozen=True)
class AccountInfo:
    balance: int  # confirmed and spendable
    nonce: int  # confirmed transfers sent
    next_nonce: int  # counting waiting transfers too
    pending_out: int  # waiting transfers from this address (amounts + fees)
    pending_in: int  # waiting transfers to this address
    immature: int  # mining rewards not spendable yet

    @property
    def available(self) -> int:
        return self.balance - self.pending_out


@dataclass(frozen=True)
class HistoryItem:
    tx: Transaction
    location: TxLocation | None  # None while waiting in the mempool
    time: int | None


class NodeService:
    def __init__(
        self,
        params: NetworkParams,
        store: Store,
        *,
        min_fee_per_byte: int = 1,
        log: Callable[[str], None] = print_now,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.params = params
        self.store = store
        self.chain = ChainManager(store, params, clock=clock)
        self.mempool = Mempool(params, store, min_fee_per_byte=min_fee_per_byte, clock=clock)
        self.events = EventBus()
        self.log = log
        self.clock = clock
        self.tip_version = 0  # bumps whenever the active chain changes (miners watch it)

    @classmethod
    def open(cls, params: NetworkParams, base: Path | None = None, **options) -> "NodeService":
        folder = network_dir(params.name, base)
        folder.mkdir(parents=True, exist_ok=True)
        return cls(params, Store(folder / CHAIN_FILE), **options)

    def close(self) -> None:
        self.store.close()

    def address_text(self, payload: bytes) -> str:
        return encode_address(payload, self.params.address_prefix)

    # --- changes ------------------------------------------------------------

    def submit_block(self, block: Block, *, source: str, note: str = "") -> SubmitResult:
        result = self.chain.submit_block(block)
        self.mempool.update(result.connected, result.disconnected)
        if result.connected or result.disconnected:
            self.tip_version += 1
        if result.disconnected:
            self.log(f"chain switch: undid {len(result.disconnected)} block(s), "
                     f"applied {len(result.connected)} from another branch")
            self.events.publish("blocks", {"event": "reorg", "data": {
                "undone": [b.block_id.hex() for b in result.disconnected],
                "applied": [b.block_id.hex() for b in result.connected],
            }})
        for connected in result.connected:
            self._announce_block(connected, source, note if connected.block_id == block.block_id else "")
        return result

    def submit_transaction(self, tx: Transaction) -> MempoolEntry:
        entry = self.mempool.add(tx)
        assert isinstance(tx, Transfer)
        data = {"txid": tx.txid.hex(), "from": self.address_text(tx.sender), "fee": amount(tx.fee),
                "outputs": [{"address": self.address_text(o.address), "amount": amount(o.amount)} for o in tx.outputs]}
        self.events.publish("mempool", {"event": "tx", "data": data})
        for address in {tx.sender, *(o.address for o in tx.outputs)}:
            text = self.address_text(address)
            self.events.publish(f"address:{text}", {"event": "address", "data": {
                "address": text, "txid": tx.txid.hex(), "status": "pending"}})
        return entry

    def template(self, miner: bytes, *, memo: bytes = b"") -> Block:
        return build_template(self.chain, self.mempool, miner, now=int(self.clock()), memo=memo)

    def _announce_block(self, block: Block, source: str, note: str) -> None:
        header = block.header
        parent = self.store.header(header.prev_id).header
        genesis = self.chain.genesis.header
        behind = (header.timestamp - genesis.timestamp) - header.height * self.params.target_spacing
        schedule = f"{abs(behind)}s {'behind' if behind > 0 else 'ahead of'} schedule" if behind else "on schedule"
        self.log(
            f"{time.strftime('%H:%M:%S')}  #{header.height:<6} {block.block_id.hex()[:12]}  "
            f"txs {len(block.transactions) - 1:<3} difficulty {difficulty(header.target, self.params):8.2f}  "
            f"+{header.timestamp - parent.timestamp}s  {schedule}  [{source}{', ' + note if note else ''}]"
        )
        self.events.publish("blocks", {"event": "block", "data": {
            "height": header.height, "id": block.block_id.hex(), "time": header.timestamp,
            "txs": len(block.transactions) - 1, "difficulty": difficulty(header.target, self.params)}})
        touched: set[bytes] = set()
        for tx in block.transactions:
            if isinstance(tx, Coinbase):
                touched.add(tx.address)
            else:
                touched.update({tx.sender, *(o.address for o in tx.outputs)})
            for address in touched:
                text = self.address_text(address)
                self.events.publish(f"address:{text}", {"event": "address", "data": {
                    "address": text, "txid": tx.txid.hex(), "status": "confirmed", "height": header.height}})
            touched.clear()

    # --- questions ----------------------------------------------------------

    def account(self, address: bytes) -> AccountInfo:
        confirmed = self.chain.get_account(address)
        pending = self.mempool.pending_for(address)
        pending_in = sum(
            o.amount for e in self.mempool.entries() for o in e.tx.outputs if o.address == address
        )
        tip = self.chain.tip_height
        immature = 0
        for height in self.store.coinbase_heights(address, above=tip - self.params.coinbase_maturity):
            immature += self.chain.main_block(height).coinbase.amount
        return AccountInfo(
            balance=confirmed.balance,
            nonce=confirmed.nonce,
            next_nonce=confirmed.nonce + len(pending),
            pending_out=sum(e.tx.total_spent for e in pending),
            pending_in=pending_in,
            immature=immature,
        )

    def history(self, address: bytes, limit: int = 50) -> list[HistoryItem]:
        """Waiting transfers first, then confirmed transactions, newest first."""
        items = [
            HistoryItem(e.tx, None, None)
            for e in sorted(self.mempool.entries(), key=lambda e: -e.added)
            if e.tx.sender == address or any(o.address == address for o in e.tx.outputs)
        ][:limit]
        for height, position, _ in self.store.address_history(address, limit - len(items)):
            block = self.chain.main_block(height)
            location = TxLocation(block.block_id, height, position)
            items.append(HistoryItem(block.transactions[position], location, block.header.timestamp))
        return items

    def find_transaction(self, txid: bytes) -> HistoryItem | None:
        location = self.store.find_tx(txid)
        if location is not None:
            block = self.store.block(location.block_id)
            return HistoryItem(block.transactions[location.position], location, block.header.timestamp)
        entry = self.mempool.get(txid)
        return HistoryItem(entry.tx, None, None) if entry else None

    def confirmations(self, height: int) -> int:
        return self.chain.tip_height - height + 1

    def describe(self) -> str:
        tip = self.chain.tip.header
        return (
            f"network {self.params.name}, genesis {self.chain.genesis.block_id.hex()[:16]}, "
            f"tip #{tip.height} {tip.block_id.hex()[:16]}, difficulty {difficulty(tip.target, self.params):.2f}, "
            f"min fee {format_amount(self.mempool.min_fee_per_byte)} TC/byte"
        )
