"""The mempool: transfers this node has accepted that aren't in a block yet.

These are node rules ("policy"), not consensus: every node chooses its own limits.

- A transfer must be valid on top of the current tip plus the sender's other
  waiting transfers: nonces follow on without gaps, and the sender's confirmed
  balance covers all of them. (Coins received but not yet confirmed can't be
  spent yet.)
- It must pay at least `min_fee_per_byte` x its size. The default, 1 base unit
  per byte, is 0.000146 TC for a simple payment. 0 accepts free transfers.
- A waiting transfer can be replaced by one with the same sender and nonce that
  pays at least 25% more per byte (to fix a fee that was too low).
- When the mempool is full, the lowest fee per byte leaves first; transfers
  older than `expiry` seconds are dropped.
- Miners take transfers highest fee per byte first, each sender's in nonce order.
"""

import heapq
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

from ..core.block import Block
from ..core.errors import ValidationError
from ..core.params import NetworkParams
from ..core.state import StateView
from ..core.tx import Transaction, Transfer, check_transaction


@dataclass(frozen=True)
class MempoolEntry:
    tx: Transfer
    size: int
    added: float

    @property
    def fee_rate(self) -> Fraction:
        return Fraction(self.tx.fee, self.size)


class Mempool:
    REPLACEMENT_FACTOR = Fraction(5, 4)

    def __init__(
        self,
        params: NetworkParams,
        state: StateView,
        *,
        min_fee_per_byte: int = 1,
        max_bytes: int = 50_000_000,
        max_per_sender: int = 64,
        expiry: int = 14 * 24 * 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.params = params
        self.state = state
        self.min_fee_per_byte = min_fee_per_byte
        self.max_bytes = max_bytes
        self.max_per_sender = max_per_sender
        self.expiry = expiry
        self.clock = clock
        self._by_id: dict[bytes, MempoolEntry] = {}
        self._by_sender: dict[bytes, dict[int, MempoolEntry]] = {}
        self._bytes = 0

    # --- reading ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, txid: bytes) -> bool:
        return txid in self._by_id

    def get(self, txid: bytes) -> MempoolEntry | None:
        return self._by_id.get(txid)

    def entries(self) -> list[MempoolEntry]:
        return list(self._by_id.values())

    @property
    def size_bytes(self) -> int:
        return self._bytes

    def pending_for(self, address: bytes) -> list[MempoolEntry]:
        pending = self._by_sender.get(address, {})
        return [pending[nonce] for nonce in sorted(pending)]

    def next_nonce(self, address: bytes) -> int:
        """The nonce a new transfer from `address` should use."""
        return self.state.get_account(address).nonce + len(self._by_sender.get(address, {}))

    # --- adding -------------------------------------------------------------

    def add(self, tx: Transaction) -> MempoolEntry:
        """Accept a transfer or raise ValidationError explaining why not."""
        if not isinstance(tx, Transfer):
            raise ValidationError("not-a-transfer", "only transfers can wait in the mempool")
        if tx.txid in self._by_id:
            raise ValidationError("already-known")
        check_transaction(tx, self.params)
        return self._insert(tx, self.clock())

    def _insert(self, tx: Transfer, added: float) -> MempoolEntry:
        """Policy checks and insertion; the signature has already been checked."""
        size = tx.size
        if tx.fee < self.min_fee_per_byte * size:
            raise ValidationError("fee-too-low", f"this node needs at least {self.min_fee_per_byte * size}")
        account = self.state.get_account(tx.sender)
        pending = self._by_sender.get(tx.sender, {})
        if tx.nonce < account.nonce:
            raise ValidationError("nonce-too-low", f"nonce {tx.nonce} is already used")

        replaced = pending.get(tx.nonce)
        if replaced is None:
            expected = account.nonce + len(pending)
            if tx.nonce != expected:
                raise ValidationError("nonce-gap", f"the next nonce is {expected}")
            if len(pending) >= self.max_per_sender:
                raise ValidationError("too-many-pending")
        elif tx.fee <= replaced.tx.fee or Fraction(tx.fee, size) < replaced.fee_rate * self.REPLACEMENT_FACTOR:
            raise ValidationError("replacement-fee-too-low", "a replacement must pay at least 25% more per byte")

        spending = tx.total_spent + sum(e.tx.total_spent for nonce, e in pending.items() if nonce != tx.nonce)
        if spending > account.balance:
            raise ValidationError("insufficient-funds", "confirmed balance doesn't cover all waiting transfers")

        if replaced is not None:
            self._remove(replaced)
        entry = MempoolEntry(tx, size, added)
        self._by_id[tx.txid] = entry
        self._by_sender.setdefault(tx.sender, {})[tx.nonce] = entry
        self._bytes += size
        self._trim_to_size()
        if tx.txid not in self._by_id:
            raise ValidationError("mempool-full", "fee per byte too low to fit")
        return entry

    def _remove(self, entry: MempoolEntry) -> None:
        del self._by_id[entry.tx.txid]
        pending = self._by_sender[entry.tx.sender]
        del pending[entry.tx.nonce]
        if not pending:
            del self._by_sender[entry.tx.sender]
        self._bytes -= entry.size

    def _trim_to_size(self) -> None:
        while self._bytes > self.max_bytes:
            # Only a sender's last transfer can go without leaving a nonce gap.
            last_of_each = (pending[max(pending)] for pending in self._by_sender.values())
            self._remove(min(last_of_each, key=lambda e: (e.fee_rate, -e.added)))

    # --- keeping up with the chain ------------------------------------------

    def update(self, connected: list[Block], disconnected: list[Block]) -> None:
        """Call after the active chain changed (ChainManager's SubmitResult lists)."""
        confirmed = {tx.txid for block in connected for tx in block.transactions}
        returning = [
            tx for block in disconnected for tx in block.transactions[1:]
            if tx.txid not in confirmed and isinstance(tx, Transfer)
        ]
        if disconnected:
            # Undoing blocks can lower anyone's balance: recheck everything.
            senders = set(self._by_sender) | {tx.sender for tx in returning}
        else:
            # New blocks only raise balances, except for senders whose transfers were included.
            senders = {tx.sender for block in connected for tx in block.transactions[1:]}

        candidates: list[tuple[Transfer, float]] = [(tx, self.clock()) for tx in returning]
        for sender in senders:
            for entry in self.pending_for(sender):
                candidates.append((entry.tx, entry.added))
                self._remove(entry)
        for tx, added in sorted(candidates, key=lambda item: (item[0].sender, item[0].nonce)):
            if tx.txid in confirmed or tx.txid in self._by_id:
                continue
            try:
                self._insert(tx, added)
            except ValidationError:
                pass  # no longer valid on the new chain

    def expire(self) -> int:
        """Drop transfers older than `expiry` (and their sender's later ones). Returns how many."""
        cutoff = self.clock() - self.expiry
        removed = 0
        for sender in list(self._by_sender):
            pending = self.pending_for(sender)
            first_old = next((i for i, e in enumerate(pending) if e.added < cutoff), None)
            if first_old is not None:
                for entry in pending[first_old:]:
                    self._remove(entry)
                    removed += 1
        return removed

    # --- mining -------------------------------------------------------------

    def select(self, max_bytes: int) -> list[Transfer]:
        """Best-paying transfers that fit in `max_bytes`, in a valid block order."""
        heap = []
        for sender, pending in self._by_sender.items():
            first = pending[min(pending)]
            heapq.heappush(heap, (-first.fee_rate, sender, first.tx.nonce))
        chosen: list[Transfer] = []
        used = 0
        while heap:
            _, sender, nonce = heapq.heappop(heap)
            entry = self._by_sender[sender][nonce]
            if used + entry.size > max_bytes:
                continue  # this sender's later transfers need this one first
            chosen.append(entry.tx)
            used += entry.size
            following = self._by_sender[sender].get(nonce + 1)
            if following is not None:
                heapq.heappush(heap, (-following.fee_rate, sender, following.tx.nonce))
        return chosen
