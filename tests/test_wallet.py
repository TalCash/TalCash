import json

import pytest

from technocoin.cli import main
from technocoin.core.params import MAINNET, REGTEST
from technocoin.core.tx import check_transaction
from technocoin.crypto import mnemonic
from technocoin.crypto.address import decode_address
from technocoin.wallet import keystore
from technocoin.wallet.keystore import INSECURE_FAST, WrongPassword
from technocoin.wallet.wallet import Wallet, WalletError

PASSWORD = "correct horse battery"
TEST_PHRASE = " ".join(["abandon"] * 11 + ["about"])


def make(tmp_path, params=REGTEST, name="wallet.json"):
    return Wallet.create(tmp_path / name, params, PASSWORD, strength=INSECURE_FAST)


def test_create_and_reload(tmp_path):
    wallet, phrase = make(tmp_path)
    assert len(phrase.split()) == 24 and mnemonic.is_valid(phrase)
    [first] = wallet.addresses
    assert first.index == 0 and first.address.startswith("tr1")
    decode_address(first.address, REGTEST.address_prefix)

    loaded = Wallet.load(tmp_path / "wallet.json")
    assert loaded.addresses == wallet.addresses
    assert loaded.params is REGTEST
    assert loaded.reveal_passphrase(PASSWORD) == phrase


def test_the_file_never_contains_the_words(tmp_path):
    wallet, phrase = make(tmp_path)
    text = (tmp_path / "wallet.json").read_text()
    # Single words can't be checked: "address", "index", "network" and "salt" are BIP39 words
    # that also appear in the file's layout. Two consecutive words never would.
    words = phrase.split()
    for first, second in zip(words, words[1:]):
        assert f"{first} {second}" not in text
    assert json.loads(text)["encrypted"]["kdf"] == "argon2id"


def test_wrong_password(tmp_path):
    wallet, _ = make(tmp_path)
    with pytest.raises(WrongPassword):
        wallet.reveal_passphrase("not the password")
    with pytest.raises(WrongPassword):
        wallet.new_address("not the password")


def test_tampered_file_is_detected(tmp_path):
    make(tmp_path)
    path = tmp_path / "wallet.json"
    data = json.loads(path.read_text())
    blob = bytearray.fromhex(data["encrypted"]["data"])
    blob[-1] ^= 1
    data["encrypted"]["data"] = blob.hex()
    path.write_text(json.dumps(data))
    with pytest.raises(WrongPassword):
        Wallet.load(path).reveal_passphrase(PASSWORD)


def test_swapped_address_in_file_is_detected(tmp_path):
    """Malware could edit the clear-text address list to redirect payments; unlocking catches it."""
    make(tmp_path)
    other, _ = make(tmp_path, name="other.json")
    path = tmp_path / "wallet.json"
    data = json.loads(path.read_text())
    data["addresses"][0]["address"] = other.addresses[0].address
    path.write_text(json.dumps(data))
    with pytest.raises(WalletError, match="does not match"):
        Wallet.load(path).check_password(PASSWORD)


def test_refuses_to_overwrite(tmp_path):
    make(tmp_path)
    with pytest.raises(WalletError, match="already exists"):
        make(tmp_path)


def test_restore_gives_the_same_addresses(tmp_path):
    wallet, phrase = make(tmp_path)
    wallet.new_address(PASSWORD)
    wallet.new_address(PASSWORD)
    restored = Wallet.restore(tmp_path / "restored.json", REGTEST, phrase.upper(), "other password",
                              strength=INSECURE_FAST)
    restored.new_address("other password")
    restored.new_address("other password")
    assert [a.address for a in restored.addresses] == [a.address for a in wallet.addresses]
    assert [a.index for a in Wallet.load(tmp_path / "wallet.json").addresses] == [0, 1, 2]


def test_restore_rejects_a_bad_passphrase(tmp_path):
    with pytest.raises(ValueError, match="checksum"):
        Wallet.restore(tmp_path / "w.json", REGTEST, " ".join(["abandon"] * 12), PASSWORD, strength=INSECURE_FAST)
    assert not (tmp_path / "w.json").exists()


def test_address_derivation_is_pinned(tmp_path):
    """If this changes, every existing wallet would show different addresses."""
    wallet = Wallet.restore(tmp_path / "w.json", MAINNET, TEST_PHRASE, PASSWORD, strength=INSECURE_FAST)
    wallet.new_address(PASSWORD)
    assert [a.address for a in wallet.addresses] == [
        "tc1MuqRsqNVaNmYnXpG4jDAyga1ZWCXrvjue",
        "tc14h3F9WrhG3ytK6BctPrsqdWktEzoQ6pPi",
    ]


def test_sign_transfer(tmp_path):
    wallet, _ = make(tmp_path)
    receiver = wallet.new_address(PASSWORD).address
    tx = wallet.sign_transfer(PASSWORD, index=0, nonce=0, fee=150, outputs=[(receiver, 2_500_000)], memo=b"hi")
    check_transaction(tx, REGTEST)
    assert tx.sender == decode_address(wallet.addresses[0].address, REGTEST.address_prefix)
    with pytest.raises(ValueError, match="start with"):
        wallet.sign_transfer(PASSWORD, index=0, nonce=0, fee=0, outputs=[("tc1abc", 1)])
    with pytest.raises(WalletError, match="no address"):
        wallet.sign_transfer(PASSWORD, index=9, nonce=0, fee=0, outputs=[(receiver, 1)])


def test_keystore_round_trip():
    blob = keystore.encrypt(b"secret", "pw", INSECURE_FAST)
    assert keystore.decrypt(blob, "pw") == b"secret"
    with pytest.raises(ValueError, match="memory"):
        keystore.decrypt({**blob, "memlimit": 1 << 40}, "pw")


def test_cli_create_and_list(tmp_path, monkeypatch, capsys):
    """Runs the real command with the real (slow) password stretching."""
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["--network", "regtest", "--datadir", str(tmp_path), "wallet", "create"]) == 0
    created = capsys.readouterr().out
    assert "Write these words on paper" in created
    address = next(line.split()[-1] for line in created.splitlines() if line.startswith("Address:"))

    assert main(["--network", "regtest", "--datadir", str(tmp_path), "wallet", "addresses"]) == 0
    assert address in capsys.readouterr().out

    # Opening a regtest wallet as mainnet is refused with a helpful message.
    assert main(["--datadir", str(tmp_path), "wallet", "--file", str(tmp_path / "regtest" / "wallet.json"),
                 "addresses"]) == 1
    assert "use --network regtest" in capsys.readouterr().err
