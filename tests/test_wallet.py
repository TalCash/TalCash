import json

import pytest

from talcash.cli import main
from talcash.core.params import MAINNET, REGTEST
from talcash.core.tx import check_transaction
from talcash.crypto import mnemonic
from talcash.crypto.address import decode_address
from talcash.wallet import keystore
from talcash.wallet.keystore import INSECURE_FAST, WrongPassword
from talcash.wallet.wallet import Wallet, WalletError, _derive_address

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


@pytest.mark.parametrize("field,value", [
    ("opslimit", 2**32 - 1), ("opslimit", 0), ("opslimit", -1),
    ("opslimit", True), ("opslimit", "3"), ("opslimit", 1.5),
    ("memlimit", 0), ("memlimit", -1), ("memlimit", True),
    ("memlimit", "8192"), ("memlimit", 8192.5),
])
def test_unsafe_wallet_kdf_is_rejected_before_work(field, value, monkeypatch):
    blob = keystore.encrypt(b"secret", "pw", INSECURE_FAST)

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe parameters reached Argon2")

    monkeypatch.setattr(keystore, "_key", forbidden)
    with pytest.raises(ValueError, match="limit"):
        keystore.decrypt({**blob, field: value}, "pw")


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


@pytest.mark.parametrize("restore", [False, True])
def test_concurrent_wallet_creation_never_overwrites(tmp_path, monkeypatch, restore):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    original = keystore.encrypt
    phrases = [mnemonic.generate(24), mnemonic.generate(24)]

    def encrypt(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait(timeout=10)  # both creators have passed the existence check
        return result

    monkeypatch.setattr(keystore, "encrypt", encrypt)
    path = tmp_path / "wallet.json"

    def create(index):
        try:
            if restore:
                wallet = Wallet.restore(path, REGTEST, phrases[index], PASSWORD, strength=INSECURE_FAST)
                return wallet, phrases[index]
            return Wallet.create(path, REGTEST, PASSWORD, strength=INSECURE_FAST)
        except WalletError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert Wallet.load(path).reveal_passphrase(PASSWORD) == winners[0][1]
    assert list(tmp_path.iterdir()) == [path]


def test_failed_wallet_update_preserves_original_and_cleans_temporary(tmp_path, monkeypatch):
    wallet, phrase = make(tmp_path)
    original = wallet.path.read_bytes()

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr("talcash.wallet.wallet.os.replace", fail)
    with pytest.raises(OSError, match="disk error"):
        wallet.save()
    assert wallet.path.read_bytes() == original
    assert Wallet.load(wallet.path).reveal_passphrase(PASSWORD) == phrase
    assert list(tmp_path.iterdir()) == [wallet.path]


def test_wallet_save_does_not_touch_predictable_temporary_path(tmp_path):
    sentinel = tmp_path / "wallet.json.tmp"
    sentinel.write_bytes(b"belongs to another process")
    wallet, _ = make(tmp_path)
    wallet.new_address(PASSWORD)
    assert sentinel.read_bytes() == b"belongs to another process"


def test_wallet_creation_fails_safely_when_publication_fails(tmp_path, monkeypatch):
    import os
    def unsupported(*args):
        raise OSError("publication unsupported")

    monkeypatch.setattr("talcash.wallet.wallet.os." + ("rename" if os.name == "nt" else "link"), unsupported)
    with pytest.raises(OSError, match="unsupported"):
        make(tmp_path)
    assert not list(tmp_path.iterdir())


def addresses_of(phrase: str, count: int) -> list[str]:
    """The first `count` addresses a passphrase gives, from a separate copy of the wallet."""
    return [_derive_address(mnemonic.to_seed(phrase, ""), index, REGTEST) for index in range(count)]


def test_discover_finds_used_addresses_up_to_the_gap(tmp_path):
    wallet, phrase = make(tmp_path)
    everything = addresses_of(phrase, 60)
    used = {everything[0], everything[3], everything[7]}
    checked = []

    def is_used(address):
        checked.append(address)
        return address in used

    added = wallet.discover(PASSWORD, is_used)
    assert [a.index for a in added] == list(range(1, 8))  # up to the last used one, gaps included
    assert checked == everything[:28]  # #7 was the last used, then 20 unused in a row
    assert [a.address for a in Wallet.load(tmp_path / "wallet.json").addresses] == everything[:8]
    assert wallet.new_address(PASSWORD).index == 8
    assert wallet.discover(PASSWORD, is_used) == []  # nothing new the second time


def test_discover_respects_the_gap(tmp_path):
    wallet, phrase = make(tmp_path)
    far = addresses_of(phrase, 26)[25]
    assert wallet.discover(PASSWORD, lambda address: address == far) == []  # 20 unused after #0: stop
    assert [a.index for a in wallet.discover(PASSWORD, lambda address: address == far, gap=30)] == list(range(1, 26))
    with pytest.raises(ValueError, match="gap"):
        wallet.discover(PASSWORD, lambda address: False, gap=0)


def test_discover_keeps_addresses_made_by_hand(tmp_path):
    wallet, _ = make(tmp_path)
    for _ in range(3):
        wallet.new_address(PASSWORD)
    assert wallet.discover(PASSWORD, lambda address: False) == []
    assert [a.index for a in wallet.addresses] == [0, 1, 2, 3]


def test_discover_checks_the_password_first(tmp_path):
    wallet, _ = make(tmp_path)
    with pytest.raises(WrongPassword):
        wallet.discover("wrong password", lambda address: pytest.fail("asked the node without the password"))
