"""Balance snapshots: one fingerprint of every balance, agreed on by the whole network.

At the end of every chunk (1,440 blocks, about a day) each node computes the
snapshot root: a Merkle root over every non-empty account, sorted by address.
Starting `snapshot_delay` blocks into the next chunk, every block header must
carry that root, and every node checks it. The delay gives nodes a few minutes
to compute the root in the background instead of stalling at the boundary.

Why: a new node can download the balances at a recent snapshot, check them
against a root buried in the (proof-of-work checked) headers, and replay only
the blocks after it instead of every block since genesis. Nodes may also delete
old blocks and keep snapshots. The Merkle tree lets a light wallet prove one
balance with a short proof.
"""

from collections.abc import Iterable

from ..crypto.address import PAYLOAD_SIZE
from .hashing import merkle_root, sha256
from .params import NetworkParams
from .state import EMPTY_ACCOUNT, Account

# Header value for blocks that come before the first snapshot.
NO_SNAPSHOT = bytes(32)
# Root of a state with no accounts (the RFC 6962 empty-tree hash).
EMPTY_STATE_ROOT = sha256(b"")


def account_leaf(address: bytes, account: Account) -> bytes:
    """address 21 | balance u64 | nonce u64 (written directly: this runs for every account daily)."""
    if len(address) != PAYLOAD_SIZE:
        raise ValueError("bad address length in state")
    return address + account.balance.to_bytes(8, "big") + account.nonce.to_bytes(8, "big")


def state_root(accounts: Iterable[tuple[bytes, Account]]) -> bytes:
    """Merkle root over all non-empty accounts, sorted by address."""
    ordered = sorted((item for item in accounts if item[1] != EMPTY_ACCOUNT), key=lambda item: item[0])
    for (first, _), (second, _) in zip(ordered, ordered[1:]):
        if first == second:
            raise ValueError("an address appears twice in the state")
    if not ordered:
        return EMPTY_STATE_ROOT
    return merkle_root([account_leaf(address, account) for address, account in ordered])


def is_snapshot_point(height: int, params: NetworkParams) -> bool:
    """True for the last block of a chunk: the state after it is a snapshot."""
    return (height + 1) % params.chunk_size == 0


def snapshot_height(height: int, params: NetworkParams) -> int | None:
    """Which snapshot the block at `height` must commit to: the height of the
    block whose resulting state is fingerprinted, or None before the first one.

    With 1,440-block chunks and a 10-block delay: blocks 0-1449 carry
    NO_SNAPSHOT, blocks 1450-2889 carry the snapshot taken after block 1439,
    blocks 2890-4329 the one after block 2879, and so on.
    """
    finished_chunks = (height - params.snapshot_delay) // params.chunk_size
    if finished_chunks < 1:
        return None
    return finished_chunks * params.chunk_size - 1
