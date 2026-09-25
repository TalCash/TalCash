"""Addresses.

Binary form (the "payload", 21 bytes) is what goes inside transactions:
    version byte (0x00) || first 20 bytes of SHA256(public key)

Text form is what people copy and paste:
    network prefix ("tc" on mainnet) || base58(payload || checksum)
    checksum = first 4 bytes of SHA256(SHA256(prefix || payload))

The prefix is part of the checksum, so a testnet address pasted into a mainnet
wallet is rejected instead of silently sending coins to the wrong network.
"""

import hashlib

from .base58 import b58decode, b58encode

ADDRESS_VERSION = 0
PAYLOAD_SIZE = 21
CHECKSUM_SIZE = 4
MAX_BASE58_LENGTH = 40  # 25 bytes are at most 35 base58 characters

# Payload that no key can ever produce: nobody can spend coins sent here.
BURN_PAYLOAD = bytes(PAYLOAD_SIZE)


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def payload_from_public_key(public_key: bytes) -> bytes:
    return bytes([ADDRESS_VERSION]) + _sha256(public_key)[:20]


def is_known_payload(payload: bytes) -> bool:
    return len(payload) == PAYLOAD_SIZE and payload[0] == ADDRESS_VERSION


def _checksum(prefix: str, payload: bytes) -> bytes:
    return _sha256(_sha256(prefix.encode("ascii") + payload))[:CHECKSUM_SIZE]


def encode_address(payload: bytes, prefix: str) -> str:
    if not is_known_payload(payload):
        raise ValueError("unknown address payload")
    return prefix + b58encode(payload + _checksum(prefix, payload))


def decode_address(text: str, prefix: str) -> bytes:
    """Return the payload of a text address, or raise ValueError explaining why it is invalid."""
    if not text.startswith(prefix):
        raise ValueError(f"address must start with {prefix!r}")
    if len(text) > len(prefix) + MAX_BASE58_LENGTH:
        raise ValueError("address is too long")  # checked first: decoding base58 slows down quadratically
    raw = b58decode(text[len(prefix):])
    if len(raw) != PAYLOAD_SIZE + CHECKSUM_SIZE:
        raise ValueError("address has the wrong length")
    payload, checksum = raw[:PAYLOAD_SIZE], raw[PAYLOAD_SIZE:]
    if checksum != _checksum(prefix, payload):
        raise ValueError("address checksum mismatch (typo, or an address from another network)")
    if not is_known_payload(payload):
        raise ValueError("unknown address version")
    return payload


def is_valid_address(text: str, prefix: str) -> bool:
    try:
        decode_address(text, prefix)
        return True
    except ValueError:
        return False
