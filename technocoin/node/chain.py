"""The node's view of the chain: which blocks it has, which chain is active, and the balances.

Lifecycle of a received block:
  1. Cheap checks right away: known already? parent known? (if not, it waits in
     the orphan pool until the parent arrives). Header against its parent
     (height, links, timestamp, difficulty, proof of work), not too far in the
     future, every transaction's signature, and the finality rule.
  2. Stored, even if it is on a side branch.
  3. If its branch now has more total work than the active chain, the node
     switches: undo active blocks back to the fork point, then apply the new
     branch's blocks one by one with full validation. All inside one database
     transaction: if any block turns out invalid, everything rolls back, the
     old chain stays active, and the bad block and its descendants are marked
     invalid.

Finality: the node never accepts a block that forks off more than
`finality_depth` blocks below its tip.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from ..core.block import Block, BlockHeader, check_block
from ..core.difficulty import block_work, median_time_past, next_target
from ..core.errors import ValidationError
from ..core.genesis import genesis_block
from ..core.params import NetworkParams
from ..core.snapshot import NO_SNAPSHOT, is_snapshot_point, snapshot_height, state_root
from ..core.state import Account, BlockContext, apply_block, check_header_against_parent, check_not_in_future
from .store import STATUS_INVALID, STATUS_STORED, STATUS_VALID, Store, StoredHeader


class Outcome(Enum):
    NEW_TIP = "new-tip"  # the block is now the tip of the active chain
    SIDE_BRANCH = "side-branch"  # stored, but its branch has no more work than the active chain
    DUPLICATE = "duplicate"  # already known
    ORPHAN = "orphan"  # parent unknown; held until the parent arrives
    INVALID = "invalid"  # breaks a rule; not stored (or marked invalid)


@dataclass
class SubmitResult:
    outcome: Outcome
    block_id: bytes
    error: ValidationError | None = None
    # Everything that changed on the active chain during this call, including
    # orphans that got connected afterwards. The mempool uses these.
    connected: list[Block] = field(default_factory=list)
    disconnected: list[Block] = field(default_factory=list)


class _ConnectFailed(Exception):
    def __init__(self, block_id: bytes, error: ValidationError) -> None:
        self.block_id = block_id
        self.error = error


class ChainManager:
    MAX_ORPHANS = 256

    def __init__(self, store: Store, params: NetworkParams, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.params = params
        self.clock = clock
        self.genesis = genesis_block(params)
        self._orphans: dict[bytes, Block] = {}  # block id -> block (insertion ordered, oldest first)
        self._init_genesis()

    def _init_genesis(self) -> None:
        stored = self.store.main_id(0)
        if stored is None:
            with self.store.transaction():
                self.store.set_meta("network", self.params.name)
                self.store.set_meta("genesis_id", self.genesis.block_id.hex())
                self.store.add_block(self.genesis, block_work(self.genesis.header.target), STATUS_VALID)
                self.store.set_main(0, self.genesis.block_id)
                self.store.index_block(self.genesis)
        elif stored != self.genesis.block_id or self.store.meta("network") != self.params.name:
            raise RuntimeError(f"{self.store.path} belongs to a different network or genesis block")

    # --- reading ------------------------------------------------------------

    @property
    def tip_height(self) -> int:
        return self.store.tip_height()

    @property
    def tip(self) -> StoredHeader:
        stored = self.store.header(self.store.main_id(self.tip_height))
        assert stored is not None
        return stored

    def main_block(self, height: int) -> Block | None:
        block_id = self.store.main_id(height)
        return self.store.block(block_id) if block_id else None

    def get_account(self, address: bytes) -> Account:
        return self.store.get_account(address)

    def finalized_height(self) -> int:
        """Blocks at or below this height can never be undone."""
        return max(0, self.tip_height - self.params.finality_depth)

    def is_on_main_chain(self, block_id: bytes) -> bool:
        stored = self.store.header(block_id)
        return stored is not None and self.store.main_id(stored.height) == block_id

    def median_time_past(self, block_id: bytes) -> int:
        """Median timestamp of this block and up to 10 ancestors (works on any branch)."""
        timestamps = []
        current = self.store.header(block_id)
        while current is not None and len(timestamps) < self.params.median_time_span:
            timestamps.append(current.header.timestamp)
            current = self.store.header(current.header.prev_id) if current.height > 0 else None
        return median_time_past(timestamps)

    def expected_target(self, parent: BlockHeader) -> int:
        return next_target(self.params, self.genesis.header, parent)

    def next_block_context(self) -> BlockContext:
        """Context for a block extending the current tip (miners build on this)."""
        return self._context(self.tip.header)

    def _context(self, parent: BlockHeader) -> BlockContext:
        """Context for a child of `parent`, which must be the tip of the active chain."""
        height = parent.height + 1
        due = height - self.params.coinbase_maturity
        matured = self.main_block(due).coinbase if due >= 0 else None
        snap = snapshot_height(height, self.params)
        if snap is None:
            root = NO_SNAPSHOT
        else:
            root = self.store.snapshot_root(snap)
            if root is None:
                raise RuntimeError(f"snapshot {snap} missing from the active chain")
        return BlockContext(
            parent=parent,
            median_time_past=self.median_time_past(parent.block_id),
            expected_target=self.expected_target(parent),
            matured_coinbase=matured,
            snapshot_root=root,
        )

    # --- receiving blocks ---------------------------------------------------

    def submit_block(self, block: Block) -> SubmitResult:
        """Process a block from a peer or a miner. Never raises for bad blocks."""
        result = self._submit_one(block)
        if result.outcome in (Outcome.NEW_TIP, Outcome.SIDE_BRANCH):
            self._adopt_orphans(block.block_id, result)
        elif result.outcome is Outcome.INVALID:
            self._drop_orphans_of(block.block_id)
        return result

    def _submit_one(self, block: Block) -> SubmitResult:
        block_id = block.block_id
        known = self.store.header(block_id)
        if known is not None:
            if known.status == STATUS_INVALID:
                return self._invalid(block_id, ValidationError("known-invalid"))
            return SubmitResult(Outcome.DUPLICATE, block_id)
        if block_id in self._orphans:
            return SubmitResult(Outcome.DUPLICATE, block_id)
        if block.height == 0:
            return self._invalid(block_id, ValidationError("bad-genesis", "genesis blocks can't be submitted"))

        parent = self.store.header(block.header.prev_id)
        if parent is None:
            self._hold_orphan(block)
            return SubmitResult(Outcome.ORPHAN, block_id)
        if parent.status == STATUS_INVALID:
            return self._invalid(block_id, ValidationError("invalid-parent"))

        try:
            # Cheapest first: proof of work costs a few milliseconds, signatures more.
            check_not_in_future(block.header, int(self.clock()), self.params)
            self._check_finality(parent)
            check_header_against_parent(
                block.header,
                parent.header,
                self.median_time_past(parent.block_id),
                self.expected_target(parent.header),
                self.params,
            )
            check_block(block, self.params)
        except ValidationError as error:
            return self._invalid(block_id, error)

        chain_work = parent.chain_work + block_work(block.header.target)
        with self.store.transaction():
            self.store.add_block(block, chain_work, STATUS_STORED)
        if chain_work > self.tip.chain_work:
            return self._switch_to(block_id)
        return SubmitResult(Outcome.SIDE_BRANCH, block_id)

    def _invalid(self, block_id: bytes, error: ValidationError) -> SubmitResult:
        return SubmitResult(Outcome.INVALID, block_id, error)

    def _check_finality(self, parent: StoredHeader) -> None:
        """Reject blocks on branches that split off below the finalized height."""
        finalized = self.finalized_height()
        current = parent
        while not self.is_on_main_chain(current.block_id):
            if current.height <= finalized:
                # The branch still differs from the active chain at a final height.
                raise ValidationError("fork-below-finality", f"history up to height {finalized} is final")
            current = self.store.header(current.header.prev_id)
        # `current` is where the branch meets the active chain.
        if current.height < finalized:
            raise ValidationError("fork-below-finality", f"history up to height {finalized} is final")

    def _hold_orphan(self, block: Block) -> None:
        if len(self._orphans) >= self.MAX_ORPHANS:
            del self._orphans[next(iter(self._orphans))]  # drop the oldest
        self._orphans[block.block_id] = block

    def _drop_orphans_of(self, block_id: bytes) -> None:
        doomed = [block_id]
        while doomed:
            parent_id = doomed.pop()
            for orphan_id in [o for o, b in self._orphans.items() if b.header.prev_id == parent_id]:
                del self._orphans[orphan_id]
                doomed.append(orphan_id)

    def _adopt_orphans(self, parent_id: bytes, result: SubmitResult) -> None:
        """Submit orphans whose parent just arrived (and their children, and so on)."""
        waiting = [parent_id]
        while waiting:
            current = waiting.pop()
            children = [b for b in self._orphans.values() if b.header.prev_id == current]
            for child in children:
                del self._orphans[child.block_id]
                child_result = self._submit_one(child)
                result.connected.extend(child_result.connected)
                result.disconnected.extend(child_result.disconnected)
                if child_result.outcome in (Outcome.NEW_TIP, Outcome.SIDE_BRANCH):
                    waiting.append(child.block_id)
                elif child_result.outcome is Outcome.INVALID:
                    self._drop_orphans_of(child.block_id)

    # --- switching chains ---------------------------------------------------

    def _switch_to(self, new_tip_id: bytes) -> SubmitResult:
        branch: list[bytes] = []
        current = self.store.header(new_tip_id)
        while not self.is_on_main_chain(current.block_id):
            branch.append(current.block_id)
            current = self.store.header(current.header.prev_id)
        branch.reverse()
        fork_height = current.height

        connected: list[Block] = []
        disconnected: list[Block] = []
        try:
            with self.store.transaction():
                while self.tip_height > fork_height:
                    disconnected.append(self._disconnect_tip())
                for block_id in branch:
                    block = self.store.block(block_id)
                    try:
                        self._connect(block)
                    except ValidationError as error:
                        raise _ConnectFailed(block_id, error) from None
                    connected.append(block)
        except _ConnectFailed as failure:
            # The transaction rolled back, so the old chain is active again, untouched.
            with self.store.transaction():
                self._mark_invalid(failure.block_id)
            return SubmitResult(Outcome.INVALID, new_tip_id, failure.error)
        return SubmitResult(Outcome.NEW_TIP, new_tip_id, connected=connected, disconnected=disconnected)

    def _connect(self, block: Block) -> None:
        tip = self.tip.header
        assert block.header.prev_id == tip.block_id
        changes = apply_block(block, self._context(tip), self.store, self.params, check_pow=False)
        self.store.write_accounts(changes.accounts)
        self.store.put_undo(block.block_id, changes.previous)
        self.store.set_main(block.height, block.block_id)
        self.store.index_block(block)
        self.store.set_status(block.block_id, STATUS_VALID)
        if is_snapshot_point(block.height, self.params):
            self.store.put_snapshot(block.height, state_root(self.store.all_accounts()))

    def _disconnect_tip(self) -> Block:
        height = self.tip_height
        block = self.store.block(self.store.main_id(height))
        self.store.write_accounts(self.store.take_undo(block.block_id))
        self.store.unindex_block(block)
        self.store.delete_main(height)
        self.store.delete_snapshot(height)
        return block

    def _mark_invalid(self, block_id: bytes) -> None:
        """Mark a block and every stored descendant invalid."""
        pending = [block_id]
        while pending:
            current = pending.pop()
            self.store.set_status(current, STATUS_INVALID)
            pending.extend(self.store.children(current))
