"""SLIP-0010 key derivation for Ed25519: one seed -> many private keys.

TechnoCoin wallets derive key number `index` at path m/44'/84184'/account'/index'
(every level hardened, which Ed25519 requires).
"""

import hashlib
import hmac

HARDENED = 0x80000000
COIN_TYPE = 84184


def master_key(seed: bytes) -> tuple[bytes, bytes]:
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def derive_child(key: bytes, chain_code: bytes, index: int) -> tuple[bytes, bytes]:
    if index < HARDENED:
        raise ValueError("Ed25519 supports hardened derivation only")
    data = b"\x00" + key + index.to_bytes(4, "big")
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def derive_path(seed: bytes, path: list[int]) -> bytes:
    key, chain_code = master_key(seed)
    for index in path:
        key, chain_code = derive_child(key, chain_code, index)
    return key


def wallet_private_key(seed: bytes, index: int = 0, account: int = 0) -> bytes:
    return derive_path(seed, [44 | HARDENED, COIN_TYPE | HARDENED, account | HARDENED, index | HARDENED])
