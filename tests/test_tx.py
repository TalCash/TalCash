from dataclasses import replace

import pytest

from talcash.core.amounts import COIN, MAX_AMOUNT
from talcash.core.errors import DecodeError, ValidationError
from talcash.core.params import MAINNET, REGTEST
from talcash.core.tx import Coinbase, Output, Transfer, check_transaction, transaction_from_bytes

from chainutil import make_transfer, named_key

ALICE, BOB, CAROL = named_key("alice"), named_key("bob"), named_key("carol")


def sample_transfer(**changes) -> Transfer:
    tx = make_transfer(REGTEST, ALICE, nonce=3, outputs=[(BOB.address, 5 * COIN), (CAROL.address, 2 * COIN)],
                       fee=150, memo=b"thanks")
    return replace(tx, **changes) if changes else tx


def test_transfer_round_trip():
    tx = sample_transfer()
    data = tx.serialize()
    assert transaction_from_bytes(data) == tx
    assert tx.size == len(data)
    assert tx.txid == transaction_from_bytes(data).txid
    assert tx.sender == ALICE.address
    assert tx.total_spent == 7 * COIN + 150
    check_transaction(tx, REGTEST)


def test_typical_transfer_size():
    tx = make_transfer(REGTEST, ALICE, 0, [(BOB.address, COIN)], fee=1)
    assert tx.size == 146


def test_coinbase_round_trip_and_unique_per_height():
    cb = Coinbase(REGTEST.network_id, 7, ALICE.address, 10 * COIN, b"hi")
    assert transaction_from_bytes(cb.serialize()) == cb
    assert replace(cb, height=8).txid != cb.txid
    check_transaction(cb, REGTEST)


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": 4},
        {"fee": 151},
        {"memo": b"thanks!"},
        {"network_id": MAINNET.network_id},
        # The old node never signed receiver amounts; now moving coins between receivers breaks the signature.
        {"outputs": (Output(BOB.address, 2 * COIN), Output(CAROL.address, 5 * COIN))},
        {"outputs": (Output(BOB.address, 7 * COIN),)},
        {"outputs": (Output(CAROL.address, 5 * COIN), Output(BOB.address, 2 * COIN))},
    ],
)
def test_signature_covers_every_field(change):
    tampered = sample_transfer(**change)
    assert not tampered.has_valid_signature()


def test_tampered_transfer_fails_validation():
    with pytest.raises(ValidationError, match="bad-signature"):
        check_transaction(sample_transfer(fee=151), REGTEST)


def test_sign_requires_the_senders_key():
    unsigned = replace(sample_transfer(), signature=bytes(64))
    with pytest.raises(ValueError):
        unsigned.sign(BOB.private_key)


def test_wrong_network_is_rejected():
    tx = make_transfer(MAINNET, ALICE, 0, [(BOB.address, COIN)])
    with pytest.raises(ValidationError, match="wrong-network"):
        check_transaction(tx, REGTEST)


def test_zero_amount_output_is_rejected():
    tx = make_transfer(REGTEST, ALICE, 0, [(BOB.address, 0)])
    with pytest.raises(ValidationError, match="bad-amount"):
        check_transaction(tx, REGTEST)


def test_negative_amounts_cannot_even_be_encoded():
    tx = replace(sample_transfer(), outputs=(Output(BOB.address, -5),))
    with pytest.raises(ValueError):
        tx.serialize()


def test_output_count_limits():
    with pytest.raises(ValidationError, match="bad-output-count"):
        check_transaction(make_transfer(REGTEST, ALICE, 0, []), REGTEST)
    many = [(BOB.address, 1)] * 256
    with pytest.raises(ValueError):
        make_transfer(REGTEST, ALICE, 0, many)


def test_unknown_address_version_is_rejected():
    tx = make_transfer(REGTEST, ALICE, 0, [(b"\x01" + bytes(20), COIN)])
    with pytest.raises(ValidationError, match="bad-address"):
        check_transaction(tx, REGTEST)


def test_total_overflow_is_rejected():
    tx = make_transfer(REGTEST, ALICE, 0, [(BOB.address, MAX_AMOUNT), (CAROL.address, 1)])
    with pytest.raises(ValidationError, match="amount-overflow"):
        check_transaction(tx, REGTEST)


def test_decode_errors():
    data = sample_transfer().serialize()
    with pytest.raises(DecodeError, match="version"):
        transaction_from_bytes(b"\x02" + data[1:])
    with pytest.raises(DecodeError, match="type"):
        transaction_from_bytes(data[:2] + b"\x07" + data[3:])
    with pytest.raises(DecodeError, match="trailing"):
        transaction_from_bytes(data + b"\x00")
    with pytest.raises(DecodeError):
        transaction_from_bytes(data[:-1])
