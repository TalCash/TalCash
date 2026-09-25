"""Rebuild a node's database from its block files alone (`tc node --reindex`).

The block files are the permanent record; the database is an index plus
balances. This throws the database away and replays every block from the
files, checking every rule again: sealed chunks first (fast, a whole chunk per
database transaction), then the recent single-block files (any branch).
"""

import time
from collections.abc import Callable
from pathlib import Path

from ..core.block import block_from_bytes
from ..core.params import NetworkParams
from .chain import ChainManager, Outcome
from .network import CHAIN_FILE
from .store import Store


def reindex(params: NetworkParams, folder: Path, log: Callable[[str], None] = print) -> int:
    """Returns the height of the rebuilt chain."""
    for suffix in ("", "-wal", "-shm"):
        (folder / (CHAIN_FILE + suffix)).unlink(missing_ok=True)
    store = Store(folder / CHAIN_FILE)
    try:
        chain = ChainManager(store, params)
        started = time.monotonic()
        blocks = 0
        for index in store.files.chunk_indexes():
            if index != store.sealed_chunks():
                log(f"chunk {index} is missing its predecessor; stopping at chunk {store.sealed_chunks()}")
                break
            blocks += chain.import_chunk(store.files.chunk_bytes(index))
            log(f"chunk {index} imported (height {chain.tip_height})")
        for height, block_id in store.files.recent_blocks():
            if height == 0 or chain.is_on_main_chain(block_id):
                continue
            result = chain.submit_block(block_from_bytes(store.files.read_recent(height, block_id)))
            if result.outcome in (Outcome.NEW_TIP, Outcome.SIDE_BRANCH):
                blocks += 1
            elif result.outcome is Outcome.INVALID:
                log(f"skipped invalid block file at height {height}: {result.error}")
        seconds = time.monotonic() - started
        log(f"rebuilt the database from files: {blocks} blocks in {seconds:.1f}s, tip #{chain.tip_height}")
        return chain.tip_height
    finally:
        store.close()
