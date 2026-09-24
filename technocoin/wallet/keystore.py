"""Password encryption for wallet secrets.

The password is stretched with Argon2id (deliberately slow and memory-hungry,
so guessing passwords is expensive) into a key for XSalsa20-Poly1305
(libsodium's secretbox), which also detects a wrong password or any tampering.
"""

import unicodedata
from dataclasses import dataclass

from nacl import pwhash, secret, utils
from nacl.exceptions import CryptoError

KDF_NAME = "argon2id"
CIPHER_NAME = "xsalsa20-poly1305"
_MAX_MEMLIMIT = 1 << 30  # refuse wallet files that demand absurd memory


class WrongPassword(Exception):
    def __init__(self) -> None:
        super().__init__("wrong password (or the wallet file is damaged)")


@dataclass(frozen=True)
class KdfStrength:
    opslimit: int
    memlimit: int


# About 0.6 s and 256 MiB per unlock.
MODERATE = KdfStrength(pwhash.argon2id.OPSLIMIT_MODERATE, pwhash.argon2id.MEMLIMIT_MODERATE)
# Only for tests: no protection at all against guessing.
INSECURE_FAST = KdfStrength(pwhash.argon2id.OPSLIMIT_MIN, pwhash.argon2id.MEMLIMIT_MIN)


def _key(password: str, salt: bytes, strength: KdfStrength) -> bytes:
    normalized = unicodedata.normalize("NFKC", password).encode("utf-8")
    return pwhash.argon2id.kdf(
        secret.SecretBox.KEY_SIZE, normalized, salt, opslimit=strength.opslimit, memlimit=strength.memlimit
    )


def encrypt(plaintext: bytes, password: str, strength: KdfStrength = MODERATE) -> dict:
    salt = utils.random(pwhash.argon2id.SALTBYTES)
    box = secret.SecretBox(_key(password, salt, strength))
    return {
        "kdf": KDF_NAME,
        "salt": salt.hex(),
        "opslimit": strength.opslimit,
        "memlimit": strength.memlimit,
        "cipher": CIPHER_NAME,
        "data": bytes(box.encrypt(plaintext)).hex(),  # nonce || ciphertext || tag
    }


def decrypt(blob: dict, password: str) -> bytes:
    if blob.get("kdf") != KDF_NAME or blob.get("cipher") != CIPHER_NAME:
        raise ValueError("unsupported wallet encryption")
    strength = KdfStrength(int(blob["opslimit"]), int(blob["memlimit"]))
    if strength.memlimit > _MAX_MEMLIMIT:
        raise ValueError("wallet file asks for too much memory")
    box = secret.SecretBox(_key(password, bytes.fromhex(blob["salt"]), strength))
    try:
        return box.decrypt(bytes.fromhex(blob["data"]))
    except CryptoError:
        raise WrongPassword() from None
