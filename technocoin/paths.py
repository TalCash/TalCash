import os
from pathlib import Path


def base_dir() -> Path:
    """Where TechnoCoin keeps its files: $TECHNOCOIN_HOME, or ~/.technocoin."""
    configured = os.environ.get("TECHNOCOIN_HOME")
    return Path(configured) if configured else Path.home() / ".technocoin"


def network_dir(network: str, base: Path | None = None) -> Path:
    return (base or base_dir()) / network


def default_wallet_path(network: str, base: Path | None = None) -> Path:
    return network_dir(network, base) / "wallet.json"
