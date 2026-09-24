"""Chain state and the state transition: how a block changes balances.

State = for every address, a balance and a nonce (how many transfers it has sent).
Balances are never stored as history; they are the result of applying every
block in order. A node that applies the same blocks always ends up with the same
state, and every change records the previous values so a block can be undone
when the node switches to a better chain.
"""

from dataclasses import dataclass
from typing import Protocol

from .amounts import MAX_AMOUNT
from .block import Block, BlockHeader, check_block
from .errors import ValidationError
from .params import NetworkParams
from .pow import meets_target
from .tx import Coinbase, Transfer


@dataclass(frozen=True)
class Account:
    balance: int = 0
    nonce: int = 0


EMPTY_ACCOUNT = Account()


class StateView(Protocol):
    def get_account(self, address: bytes) -> Account: ...


@dataclass(frozen=True)
class BlockContext:
    """Facts about the chain a block extends. The node computes these."""

    parent: BlockHeader
    median_time_past: int  # of the parent and up to 10 blocks before it
    expected_target: int  # difficulty.next_target(...) for this block
    matured_coinbase: Coinbase | None  # coinbase of the block `coinbase_maturity` below this one
    snapshot_root: bytes  # root of snapshot.snapshot_height(height), or snapshot.NO_SNAPSHOT


@dataclass
class StateChanges:
    accounts: dict[bytes, Account]  # new value of every account the block touched
    previous: dict[bytes, Account]  # value before the block (for undo)
    fees: int


class _Overlay:
    """Reads from a state, records writes without touching it."""

    def __init__(self, base: StateView) -> None:
        self._base = base
        self.accounts: dict[bytes, Account] = {}
        self.previous: dict[bytes, Account] = {}

    def get(self, address: bytes) -> Account:
        if address in self.accounts:
            return self.accounts[address]
        return self._base.get_account(address)

    def set(self, address: bytes, account: Account) -> None:
        if address not in self.previous:
            self.previous[address] = self._base.get_account(address)
        self.accounts[address] = account

    def credit(self, address: bytes, amount: int) -> None:
        account = self.get(address)
        balance = account.balance + amount
        if balance > MAX_AMOUNT:
            raise ValidationError("balance-overflow")
        self.set(address, Account(balance, account.nonce))


def check_header(header: BlockHeader, ctx: BlockContext, params: NetworkParams) -> None:
    """Header rules that depend on the parent, including proof of work."""
    if header.height != ctx.parent.height + 1:
        raise ValidationError("bad-height", f"expected {ctx.parent.height + 1}, got {header.height}")
    if header.prev_id != ctx.parent.block_id:
        raise ValidationError("bad-prev-id")
    if header.timestamp <= ctx.median_time_past:
        raise ValidationError("time-too-old", "timestamp must be after the median of the last 11 blocks")
    if header.target != ctx.expected_target:
        raise ValidationError("bad-target")
    if header.snapshot_root != ctx.snapshot_root:
        raise ValidationError("bad-snapshot-root")
    if not meets_target(header, params.pow):
        raise ValidationError("bad-pow")


def check_not_in_future(header: BlockHeader, now: int, params: NetworkParams) -> None:
    """Node rule for newly received blocks (not re-checked when replaying history)."""
    if header.timestamp > now + params.max_future_drift:
        raise ValidationError("time-too-new", "block timestamp is too far in the future")


def apply_block(block: Block, ctx: BlockContext, state: StateView, params: NetworkParams) -> StateChanges:
    """Validate `block` on top of `state` and return the resulting changes.

    Raises ValidationError if the block breaks any rule. `state` is not modified.
    """
    check_header(block.header, ctx, params)
    check_block(block, params)

    overlay = _Overlay(state)
    height = block.header.height

    # 1. The reward mined `coinbase_maturity` blocks ago becomes spendable now.
    due_height = height - params.coinbase_maturity
    matured = ctx.matured_coinbase
    if due_height >= 0:
        if matured is None or matured.height != due_height:
            raise ValueError(f"context must supply the coinbase of block {due_height}")
        overlay.credit(matured.address, matured.amount)
    elif matured is not None:
        raise ValueError("no coinbase can mature at this height")

    # 2. Transfers, in block order.
    fees = 0
    for tx in block.transactions[1:]:
        assert isinstance(tx, Transfer)
        sender = overlay.get(tx.sender)
        if tx.nonce != sender.nonce:
            raise ValidationError("bad-nonce", f"expected nonce {sender.nonce}, got {tx.nonce}")
        if tx.total_spent > sender.balance:
            raise ValidationError("insufficient-funds")
        overlay.set(tx.sender, Account(sender.balance - tx.total_spent, sender.nonce + 1))
        for output in tx.outputs:
            overlay.credit(output.address, output.amount)
        fees += tx.fee

    # 3. The miner claims exactly the reward plus the fees (credited at maturity).
    if block.coinbase.amount != params.block_reward + fees:
        raise ValidationError(
            "bad-coinbase-amount",
            f"expected {params.block_reward + fees}, got {block.coinbase.amount}",
        )

    return StateChanges(overlay.accounts, overlay.previous, fees)


class MemoryState:
    """A state kept in a dict. Used by tests and tools; the node keeps state in SQLite."""

    def __init__(self) -> None:
        self.accounts: dict[bytes, Account] = {}

    def get_account(self, address: bytes) -> Account:
        return self.accounts.get(address, EMPTY_ACCOUNT)

    def _write(self, values: dict[bytes, Account]) -> None:
        for address, account in values.items():
            if account == EMPTY_ACCOUNT:
                self.accounts.pop(address, None)
            else:
                self.accounts[address] = account

    def apply(self, changes: StateChanges) -> None:
        self._write(changes.accounts)

    def revert(self, changes: StateChanges) -> None:
        self._write(changes.previous)
