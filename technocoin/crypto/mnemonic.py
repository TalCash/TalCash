"""BIP39 passphrases (24 words by default), compatible with the standard.

The words encode random entropy plus a checksum; `to_seed` stretches the words
(and an optional extra passphrase) into the 64-byte wallet seed.
"""

import hashlib
import secrets
import unicodedata
from functools import cache
from importlib import resources

_VALID_ENTROPY_BYTES = (16, 20, 24, 28, 32)


@cache
def wordlist() -> tuple[str, ...]:
    text = resources.files(__package__).joinpath("english.txt").read_text(encoding="utf-8")
    words = tuple(text.split())
    assert len(words) == 2048
    return words


@cache
def _word_index() -> dict[str, int]:
    return {word: i for i, word in enumerate(wordlist())}


def generate(words: int = 24) -> str:
    entropy_bytes = words * 4 // 3
    if words % 3 or entropy_bytes not in _VALID_ENTROPY_BYTES:
        raise ValueError("word count must be 12, 15, 18, 21 or 24")
    return entropy_to_mnemonic(secrets.token_bytes(entropy_bytes))


def entropy_to_mnemonic(entropy: bytes) -> str:
    if len(entropy) not in _VALID_ENTROPY_BYTES:
        raise ValueError("entropy must be 16, 20, 24, 28 or 32 bytes")
    checksum_bits = len(entropy) * 8 // 32
    bits = int.from_bytes(entropy, "big") << checksum_bits
    bits |= hashlib.sha256(entropy).digest()[0] >> (8 - checksum_bits)
    word_count = (len(entropy) * 8 + checksum_bits) // 11
    words = wordlist()
    return " ".join(words[(bits >> (11 * (word_count - 1 - i))) & 0x7FF] for i in range(word_count))


def normalize(mnemonic: str) -> str:
    return " ".join(unicodedata.normalize("NFKD", mnemonic).lower().split())


def mnemonic_to_entropy(mnemonic: str) -> bytes:
    words = normalize(mnemonic).split(" ")
    if len(words) not in (12, 15, 18, 21, 24):
        raise ValueError("a passphrase has 12, 15, 18, 21 or 24 words")
    index = _word_index()
    bits = 0
    for word in words:
        if word not in index:
            raise ValueError(f"{word!r} is not in the word list")
        bits = (bits << 11) | index[word]
    checksum_bits = len(words) * 11 // 33
    entropy_bytes = (len(words) * 11 - checksum_bits) // 8
    entropy = (bits >> checksum_bits).to_bytes(entropy_bytes, "big")
    if bits & ((1 << checksum_bits) - 1) != hashlib.sha256(entropy).digest()[0] >> (8 - checksum_bits):
        raise ValueError("passphrase checksum mismatch (a word is wrong or out of order)")
    return entropy


def is_valid(mnemonic: str) -> bool:
    try:
        mnemonic_to_entropy(mnemonic)
        return True
    except ValueError:
        return False


def to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    salt = "mnemonic" + unicodedata.normalize("NFKD", passphrase)
    return hashlib.pbkdf2_hmac("sha512", normalize(mnemonic).encode("utf-8"), salt.encode("utf-8"), 2048)
