"""Network parameters. Everything that differs between networks lives here.

  mainnet  the real network
  testnet  public test network, same rules as mainnet
  devnet   local network for trying things on your own computer: 1-second blocks,
           everything else like mainnet but 60x faster. Each devnet gets a fresh
           genesis block when it is created (see node/network.py).
  regtest  for automated tests only: instant blocks, difficulty never changes

Mainnet and testnet genesis fields stay empty until those networks are launched.
"""

from dataclasses import dataclass

from .amounts import COIN


@dataclass(frozen=True)
class PowParams:
    """Argon2id settings for proof of work (RFC 9106, version 1.3, one lane)."""

    memory_kib: int
    iterations: int
    salt: bytes = b"TalCash/Argon2/1"  # Argon2 via libsodium needs exactly 16 bytes


def target_for(expected_hashes: int) -> int:
    """The target at which, on average, one hash in `expected_hashes` wins."""
    return (1 << 256) // expected_hashes - 1


@dataclass(frozen=True)
class NetworkParams:
    name: str
    network_id: int  # written into every transaction, so they can't be replayed on another network
    address_prefix: str
    default_port: int
    pow: PowParams
    pow_limit: int  # easiest target ever allowed
    genesis_target: int
    pow_no_retarget: bool = False  # regtest only: the target never changes

    block_reward: int = 10 * COIN
    target_spacing: int = 60  # seconds per block
    asert_half_life: int = 60 * 60  # how slowly difficulty reacts (see difficulty.py)
    coinbase_maturity: int = 100  # blocks before a block reward can be spent
    finality_depth: int = 100  # node never reorganizes deeper than this
    chunk_size: int = 1440  # blocks per chunk: one day at one block per minute
    snapshot_delay: int = 10  # blocks into a new chunk before headers must carry its balance snapshot
    chunks_per_mega_chunk: int = 365  # a mega chunk is about one year (not a consensus rule)
    max_block_size: int = 1_000_000  # bytes
    median_time_span: int = 11  # blocks used for the median-time-past rule
    max_future_drift: int = 5 * 60  # seconds a block may be ahead of the node's clock

    # Genesis block (set when the network launches).
    genesis_timestamp: int | None = None
    genesis_message: bytes = b""
    genesis_nonce: int | None = None
    genesis_id: bytes | None = None

    def __post_init__(self) -> None:
        if self.chunk_size < 2 or not 0 <= self.snapshot_delay < self.chunk_size:
            raise ValueError("need chunk_size >= 2 and 0 <= snapshot_delay < chunk_size")


# One core does about 300 Argon2id hashes per second with these settings, so the
# first mainnet blocks take about a minute for one core. ASERT takes over from there.
_ARGON2_MAINNET = PowParams(memory_kib=4096, iterations=1)

MAINNET = NetworkParams(
    name="mainnet",
    network_id=1,
    address_prefix="tc",
    default_port=64184,
    pow=_ARGON2_MAINNET,
    pow_limit=target_for(600),
    genesis_target=target_for(18_000),
)

TESTNET = NetworkParams(
    name="testnet",
    network_id=2,
    address_prefix="tt",
    default_port=64185,
    pow=_ARGON2_MAINNET,
    pow_limit=target_for(600),
    genesis_target=target_for(18_000),
)

# Mainnet's behaviour at 60x speed: one block per second, a one-minute ASERT half-life
# (60 blocks, as on mainnet), one-minute chunks. Same Argon2id work as mainnet;
# the genesis target is about one core-second.
DEVNET = NetworkParams(
    name="devnet",
    network_id=4,
    address_prefix="td",
    default_port=64187,
    pow=_ARGON2_MAINNET,
    pow_limit=target_for(10),
    genesis_target=target_for(300),
    target_spacing=1,
    asert_half_life=60,
    chunk_size=60,
    snapshot_delay=5,
    max_future_drift=10,
)

REGTEST = NetworkParams(
    name="regtest",
    network_id=3,
    address_prefix="tr",
    default_port=64186,
    pow=PowParams(memory_kib=8, iterations=1),
    pow_limit=target_for(2),
    genesis_target=target_for(2),
    pow_no_retarget=True,
    genesis_timestamp=1_767_225_600,  # 2026-01-01 00:00:00 UTC
    genesis_message=b"TalCash regtest",
    genesis_nonce=1,
    genesis_id=bytes.fromhex("ca4d99507e879bb59d28eb9d4a578d54b5c833c5d51f3e8e5a95d76faa63393b"),
)

NETWORKS = {params.name: params for params in (MAINNET, TESTNET, DEVNET, REGTEST)}
