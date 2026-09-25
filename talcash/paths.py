import os
from pathlib import Path


def base_dir() -> Path:
    """Where TalCash keeps its files: $TALCASH_HOME, or ~/.talcash."""
    configured = os.environ.get("TALCASH_HOME")
    return Path(configured) if configured else Path.home() / ".talcash"


def network_dir(network: str, base: Path | None = None) -> Path:
    return (base or base_dir()) / network


def default_wallet_path(network: str, base: Path | None = None) -> Path:
    return network_dir(network, base) / "wallet.json"
