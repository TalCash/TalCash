"""Choosing a network, including the devnet's own genesis block.

Mainnet and testnet have a fixed genesis compiled into the code. A devnet is a
throwaway local network: its genesis block is mined when it is first started,
stamped with the current time (difficulty is scheduled from genesis, so an old
genesis would make a new devnet start "months behind" and race to catch up),
and saved as genesis.json next to its chain so restarts continue the same chain.
"""

import json
import os
import time
from dataclasses import replace
from pathlib import Path

from ..core.genesis import mine_genesis
from ..core.params import DEVNET, NETWORKS, NetworkParams
from ..paths import network_dir

GENESIS_FILE = "genesis.json"
CHAIN_FILE = "chain.sqlite"


def load_params(name: str, base: Path | None = None, *, create: bool = True) -> NetworkParams:
    params = NETWORKS[name]
    if params.name != DEVNET.name:
        return params
    path = network_dir(name, base) / GENESIS_FILE
    if not path.exists():
        if not create:
            raise RuntimeError(f"no devnet at {path.parent} yet")
        _create_devnet_genesis(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return replace(
        DEVNET,
        genesis_timestamp=int(data["timestamp"]),
        genesis_message=bytes.fromhex(data["message"]),
        genesis_nonce=int(data["nonce"]),
        genesis_id=bytes.fromhex(data["id"]),
    )


def _create_devnet_genesis(path: Path) -> None:
    timestamp = int(time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(timestamp))
    block = mine_genesis(DEVNET, timestamp=timestamp, message=f"TechnoCoin devnet {stamp} UTC".encode())
    data = {
        "timestamp": timestamp,
        "message": block.coinbase.memo.hex(),
        "nonce": block.header.nonce,
        "id": block.block_id.hex(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def reset_devnet(base: Path | None = None) -> list[Path]:
    """Delete a devnet's chain and genesis (never its wallet). Returns what was removed."""
    folder = network_dir(DEVNET.name, base)
    removed = []
    for name in (GENESIS_FILE, CHAIN_FILE, CHAIN_FILE + "-wal", CHAIN_FILE + "-shm"):
        target = folder / name
        if target.exists():
            target.unlink()
            removed.append(target)
    return removed
