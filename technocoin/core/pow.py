"""Proof of work: Argon2id over the block header.

Argon2id needs a few megabytes of fast memory for every single hash. That makes
special mining chips (ASICs) pointless and keeps graphics cards' edge small, so
ordinary computers can compete. It also means the Python miner is nearly as
fast as a native one: almost all the time is spent inside libsodium.
"""

from dataclasses import replace

from nacl import pwhash
from nacl.encoding import RawEncoder

from .block import BlockHeader
from .params import PowParams

_NONCE_SIZE = 8


def pow_hash_bytes(header_bytes: bytes, params: PowParams) -> bytes:
    return pwhash.argon2id.kdf(
        32,
        header_bytes,
        params.salt,
        opslimit=params.iterations,
        memlimit=params.memory_kib * 1024,
        encoder=RawEncoder,
    )


def pow_hash(header: BlockHeader, params: PowParams) -> bytes:
    return pow_hash_bytes(header.serialize(), params)


def meets_target(header: BlockHeader, params: PowParams) -> bool:
    return int.from_bytes(pow_hash(header, params), "big") <= header.target


def mine(
    header: BlockHeader,
    params: PowParams,
    *,
    start_nonce: int = 0,
    stride: int = 1,
    max_attempts: int | None = None,
) -> BlockHeader | None:
    """Search nonces start_nonce, start_nonce + stride, ... for a winning header.

    `stride` lets several processes split the nonce space. Returns None if
    `max_attempts` runs out first.
    """
    # The nonce is the last header field, so only its 8 bytes change per attempt.
    prefix = header.serialize()[:-_NONCE_SIZE]
    target = header.target
    nonce = start_nonce
    attempts = 0
    while max_attempts is None or attempts < max_attempts:
        if nonce >= 1 << 64:
            return None
        digest = pow_hash_bytes(prefix + nonce.to_bytes(_NONCE_SIZE, "big"), params)
        if int.from_bytes(digest, "big") <= target:
            return replace(header, nonce=nonce)
        nonce += stride
        attempts += 1
    return None
