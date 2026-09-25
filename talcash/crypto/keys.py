"""Ed25519 keys (libsodium via PyNaCl). A private key is its 32-byte seed."""

import secrets

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

PRIVATE_KEY_SIZE = 32
PUBLIC_KEY_SIZE = 32
SIGNATURE_SIZE = 64


def generate_private_key() -> bytes:
    return secrets.token_bytes(PRIVATE_KEY_SIZE)


def public_key(private_key: bytes) -> bytes:
    return bytes(SigningKey(private_key).verify_key)


def sign(private_key: bytes, message: bytes) -> bytes:
    return SigningKey(private_key).sign(message).signature


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a valid signature. Never raises on malformed input."""
    if len(public_key) != PUBLIC_KEY_SIZE or len(signature) != SIGNATURE_SIZE:
        return False
    try:
        VerifyKey(public_key).verify(message, signature)
        return True
    except (BadSignatureError, ValueError):
        return False
