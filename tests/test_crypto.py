import pytest

from technocoin.crypto import hd, keys, mnemonic
from technocoin.crypto.address import (
    BURN_PAYLOAD,
    _checksum,
    decode_address,
    encode_address,
    is_valid_address,
    payload_from_public_key,
)
from technocoin.crypto.base58 import b58decode, b58encode


# --- base58 ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, text",
    [(b"", ""), (b"\x00\x00\x01", "112"), (b"hello world", "StV1DL6CwTryKyV"), (b"\x00" * 3, "111")],
)
def test_base58_vectors(raw, text):
    assert b58encode(raw) == text
    assert b58decode(text) == raw


def test_base58_rejects_bad_characters():
    for bad in ("0", "O", "I", "l", "tc!"):
        with pytest.raises(ValueError):
            b58decode(bad)


# --- BIP39 (official test vectors, passphrase "TREZOR") ------------------

def test_bip39_vectors():
    assert mnemonic.entropy_to_mnemonic(bytes(16)) == " ".join(["abandon"] * 11 + ["about"])
    assert mnemonic.entropy_to_mnemonic(b"\x7f" * 16) == (
        "legal winner thank year wave sausage worth useful legal winner thank yellow"
    )
    assert mnemonic.entropy_to_mnemonic(bytes(32)) == " ".join(["abandon"] * 23 + ["art"])
    assert mnemonic.entropy_to_mnemonic(b"\xff" * 32) == " ".join(["zoo"] * 23 + ["vote"])
    seed = mnemonic.to_seed(" ".join(["abandon"] * 11 + ["about"]), "TREZOR")
    assert seed.hex() == (
        "c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e53495531f"
        "09a6987599d18264c1e1c92f2cf141630c7a3c4ab7c81b2f001698e7463b04"
    )


def test_mnemonic_round_trip_and_generation():
    phrase = mnemonic.generate()
    assert len(phrase.split()) == 24
    assert mnemonic.is_valid(phrase)
    assert mnemonic.entropy_to_mnemonic(mnemonic.mnemonic_to_entropy(phrase)) == phrase
    assert mnemonic.generate() != phrase
    assert mnemonic.is_valid("  " + phrase.upper().replace(" ", "   ") + "\n")


def test_mnemonic_rejects_mistakes():
    words = mnemonic.generate().split()
    assert not mnemonic.is_valid(" ".join(words[:-1]))  # missing word
    assert not mnemonic.is_valid(" ".join(words[:-1] + ["notaword"]))
    # Real words, wrong checksum: "abandon" x 12 is the classic invalid phrase.
    with pytest.raises(ValueError, match="checksum"):
        mnemonic.mnemonic_to_entropy(" ".join(["abandon"] * 12))


# --- SLIP-0010 Ed25519 (official test vector 1) --------------------------

def test_slip10_vector():
    seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    key, chain = hd.master_key(seed)
    assert key.hex() == "2b4be7f19ee27bbf30c667b642d5f4aa69fd169872f8fc3059c08ebae2eb19e7"
    assert chain.hex() == "90046a93de5380a72b5e45010748567d5ea02bbf6522f979e05c0d8d8ca9fffb"
    assert keys.public_key(key).hex() == "a4b2856bfec510abab89753fac1ac0e1112364e7d250545963f135f2a33188ed"
    child, child_chain = hd.derive_child(key, chain, 0 | hd.HARDENED)
    assert child.hex() == "68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e704f1dade7a3"
    assert child_chain.hex() == "8b59aa11380b624e81507a27fedda59fea6d0b779a778918a2fd3590e16e9c69"


def test_hd_rejects_non_hardened_and_is_deterministic():
    seed = mnemonic.to_seed(mnemonic.generate())
    key, chain = hd.master_key(seed)
    with pytest.raises(ValueError):
        hd.derive_child(key, chain, 0)
    assert hd.wallet_private_key(seed, 0) == hd.wallet_private_key(seed, 0)
    assert hd.wallet_private_key(seed, 0) != hd.wallet_private_key(seed, 1)


# --- signatures ------------------------------------------------------------

def test_sign_and_verify():
    private_key = keys.generate_private_key()
    public_key = keys.public_key(private_key)
    signature = keys.sign(private_key, b"hello")
    assert keys.verify(public_key, b"hello", signature)
    assert not keys.verify(public_key, b"hello!", signature)
    assert not keys.verify(keys.public_key(keys.generate_private_key()), b"hello", signature)


def test_verify_never_raises_on_garbage():
    assert not keys.verify(b"short", b"m", bytes(64))
    assert not keys.verify(bytes(32), b"m", b"short")
    assert not keys.verify(bytes(32), b"m", bytes(64))
    assert not keys.verify(b"\xff" * 32, b"m", b"\xff" * 64)


# --- addresses ---------------------------------------------------------------

def test_address_round_trip():
    payload = payload_from_public_key(keys.public_key(keys.generate_private_key()))
    text = encode_address(payload, "tc")
    assert text.startswith("tc1")  # version byte 0 shows up as a leading "1"
    assert decode_address(text, "tc") == payload


def test_address_typo_and_network_mismatch_are_rejected():
    payload = payload_from_public_key(keys.public_key(keys.generate_private_key()))
    text = encode_address(payload, "tc")
    typo = text[:-1] + ("2" if text[-1] != "2" else "3")
    assert not is_valid_address(typo, "tc")
    assert not is_valid_address(text, "tt")
    # Same payload, just the prefix swapped: the checksum covers the prefix, so it fails.
    assert not is_valid_address("tt" + text[2:], "tt")


def test_unknown_address_version_is_rejected():
    payload = b"\x01" + bytes(20)
    text = "tc" + b58encode(payload + _checksum("tc", payload))
    with pytest.raises(ValueError, match="version"):
        decode_address(text, "tc")
    with pytest.raises(ValueError):
        encode_address(payload, "tc")


def test_burn_address():
    text = encode_address(BURN_PAYLOAD, "tc")
    assert text == "tc1111111111111111111115gbLbA"
    assert decode_address(text, "tc") == BURN_PAYLOAD
