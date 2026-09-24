"""Running a node on this computer.

For now a single node that mines on its own chain. The HTTP/WebSocket API and
peer-to-peer sync come next and will run around this same chain + mempool.
"""

import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ..core.amounts import format_amount
from ..core.block import Block
from ..core.difficulty import difficulty
from ..core.params import NetworkParams
from ..crypto.address import encode_address
from ..miner.engine import Miner
from ..paths import network_dir
from .chain import ChainManager, Outcome
from .mempool import Mempool
from .network import CHAIN_FILE
from .store import Store
from .template import build_template


def print_now(line: str) -> None:
    """print() that shows up immediately even when output goes to a file or a service log."""
    print(line, flush=True)


class LocalNode:
    def __init__(
        self,
        params: NetworkParams,
        base: Path | None = None,
        *,
        log: Callable[[str], None] = print_now,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.params = params
        self.folder = network_dir(params.name, base)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.folder / CHAIN_FILE)
        self.chain = ChainManager(self.store, params, clock=clock)
        self.mempool = Mempool(params, self.store, clock=clock)
        self.log = log
        self.clock = clock

    def close(self) -> None:
        self.store.close()

    def describe(self) -> str:
        tip = self.chain.tip.header
        return (
            f"network {self.params.name}, data in {self.folder}\n"
            f"genesis {self.chain.genesis.block_id.hex()[:16]}, tip #{tip.height} {tip.block_id.hex()[:16]}, "
            f"difficulty {difficulty(tip.target, self.params):.2f}"
        )

    def mine(self, miner: bytes, *, workers: int | None = None, blocks: int | None = None, memo: bytes = b"") -> None:
        """Mine on top of the local chain until `blocks` are found (forever if None)."""
        params = self.params
        refresh = max(1, params.target_spacing // 6)  # rebuild the template this often (fresh time, new transfers)
        found = 0
        with Miner(workers) as engine:
            self.log(f"mining with {engine.workers} worker processes, paying {encode_address(miner, params.address_prefix)}")
            last_hashes, last_time = 0, time.monotonic()
            while blocks is None or found < blocks:
                template = build_template(self.chain, self.mempool, miner, now=int(self.clock()), memo=memo)
                engine.mine(template.header, params.pow)
                nonce = engine.wait(timeout=refresh)
                if nonce is None:
                    continue
                block = Block(replace(template.header, nonce=nonce), template.transactions)
                result = self.chain.submit_block(block)
                self.mempool.update(result.connected, result.disconnected)
                if result.outcome is not Outcome.NEW_TIP:
                    self.log(f"own block rejected: {result.outcome.value} {result.error or ''}")
                    continue
                found += 1
                hashes, now = engine.total_hashes(), time.monotonic()
                rate = (hashes - last_hashes) / max(now - last_time, 1e-9)
                last_hashes, last_time = hashes, now
                self._log_block(block, rate)

    def _log_block(self, block: Block, hashrate: float) -> None:
        header = block.header
        parent = self.store.header(header.prev_id).header
        behind = (header.timestamp - self.chain.genesis.header.timestamp) - header.height * self.params.target_spacing
        schedule = f"{abs(behind)}s {'behind' if behind > 0 else 'ahead of'} schedule" if behind else "on schedule"
        self.log(
            f"{time.strftime('%H:%M:%S')}  #{header.height:<6} {block.block_id.hex()[:12]}  "
            f"txs {len(block.transactions) - 1:<3} difficulty {difficulty(header.target, self.params):8.2f}  "
            f"+{header.timestamp - parent.timestamp}s  {hashrate:7.0f} H/s  {schedule}  "
            f"reward {format_amount(block.coinbase.amount)} TC"
        )
