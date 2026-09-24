"""Nobody can change a signed payment: not a relaying node, not a miner, not an attacker.

The signature covers every byte of a transfer (sender, nonce, fee, every
receiver's address and amount, the memo, the network), and a block's merkle
root covers every transaction's bytes including its signature. So changing
even one bit anywhere must be rejected. These tests change every bit, one at
a time, and check exactly that.
"""

from collections import Counter
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from technocoin.core.amounts import COIN
from technocoin.core.block import HEADER_SIZE, Block, block_from_bytes
from technocoin.core.errors import DecodeError, ValidationError
from technocoin.core.params import REGTEST
from technocoin.core.pow import mine
from technocoin.core.tx import Output, check_transaction, transaction_from_bytes
from technocoin.crypto.address import encode_address
from technocoin.node.api import create_app
from technocoin.node.chain import ChainManager, Outcome
from technocoin.node.service import NodeService
from technocoin.node.store import Store

from chainutil import TestChain, make_transfer, named_key

PARAMS = replace(REGTEST, coinbase_maturity=3)
ALICE, BOB, CAROL, DAVE, MALLORY = (named_key(n) for n in ("alice", "bob", "carol", "dave", "mallory"))


def three_way_payment(nonce: int = 0):
    return make_transfer(PARAMS, ALICE, nonce, [(BOB.address, 5 * COIN), (CAROL.address, 2 * COIN),
                                                 (DAVE.address, 1)], fee=300, memo=b"rent for march")


def flipped(data: bytes, bit: int) -> bytes:
    mutated = bytearray(data)
    mutated[bit // 8] ^= 1 << (bit % 8)
    return bytes(mutated)


@pytest.mark.parametrize("forgery", [
    "redirect Bob's 5 TC to Mallory",
    "one base unit more for Bob",
    "one base unit less for Carol",
    "drop Dave",
    "add Mallory as a fourth receiver",
    "swap Bob's and Carol's amounts",
    "different memo",
    "no memo",
    "lower fee",
    "different nonce",
    "claim to be sent by Mallory",
])
def test_specific_forgeries_are_rejected(forgery):
    tx = three_way_payment()
    bob, carol, dave = tx.outputs
    changes = {
        "redirect Bob's 5 TC to Mallory": {"outputs": (Output(MALLORY.address, bob.amount), carol, dave)},
        "one base unit more for Bob": {"outputs": (Output(bob.address, bob.amount + 1), carol, dave)},
        "one base unit less for Carol": {"outputs": (bob, Output(carol.address, carol.amount - 1), dave)},
        "drop Dave": {"outputs": (bob, carol)},
        "add Mallory as a fourth receiver": {"outputs": (bob, carol, dave, Output(MALLORY.address, 1))},
        "swap Bob's and Carol's amounts": {"outputs": (Output(bob.address, carol.amount),
                                                       Output(carol.address, bob.amount), dave)},
        "different memo": {"memo": b"rent for april"},
        "no memo": {"memo": b""},
        "lower fee": {"fee": 299},
        "different nonce": {"nonce": 1},
        "claim to be sent by Mallory": {"sender_public_key": MALLORY.public_key},
    }[forgery]
    with pytest.raises(ValidationError, match="bad-signature"):
        check_transaction(replace(tx, **changes), PARAMS)


def test_every_single_bit_of_a_payment_is_protected():
    tx = three_way_payment()
    check_transaction(tx, PARAMS)  # the original is fine
    data = tx.serialize()
    outcomes: Counter[str] = Counter()
    for bit in range(len(data) * 8):
        try:
            forged = transaction_from_bytes(flipped(data, bit))
        except DecodeError:
            outcomes["unreadable"] += 1
            continue
        with pytest.raises(ValidationError) as rejected:
            check_transaction(forged, PARAMS)
        outcomes[rejected.value.code] += 1
    assert sum(outcomes.values()) == len(data) * 8  # every variant was rejected (1,744 for this payment)
    assert outcomes["bad-signature"] > len(data) * 8 // 2  # mostly caught by the signature itself


def test_nobody_can_change_a_payment_inside_a_mined_block():
    """A miner or relaying node that edits any bit of the transactions in a block gets it rejected."""
    ref = TestChain(PARAMS)
    ref.mine_blocks(4, miner=ALICE.address)
    block = ref.build_block([three_way_payment()])

    node = ChainManager(Store(":memory:"), PARAMS, clock=lambda: 2_000_000_000)
    for earlier in ref.blocks[1:]:
        node.submit_block(earlier)

    data = block.serialize()
    tx_bits = range(HEADER_SIZE * 8, len(data) * 8)  # everything after the header
    outcomes: Counter[str] = Counter()
    for bit in tx_bits:
        try:
            forged = block_from_bytes(flipped(data, bit))
        except DecodeError:
            outcomes["unreadable"] += 1
            continue
        result = node.submit_block(forged)
        assert result.outcome is Outcome.INVALID, bit
        outcomes[result.error.code] += 1
    assert sum(outcomes.values()) == len(tx_bits)
    assert node.tip_height == 4  # nothing forged got in
    assert node.submit_block(block).outcome is Outcome.NEW_TIP  # the untouched block is fine
    assert node.get_account(BOB.address).balance == 5 * COIN


def test_a_node_refuses_forged_payments_and_accepts_the_real_one():
    app = create_app(lambda: NodeService(PARAMS, Store(":memory:"), log=lambda line: None))
    with TestClient(app) as api:
        alice_text = encode_address(ALICE.address, PARAMS.address_prefix)
        for _ in range(4):
            template = block_from_bytes(bytes.fromhex(
                api.get("/v1/mining/template", params={"address": alice_text}).json()["hex"]))
            mined = Block(mine(template.header, PARAMS.pow), template.transactions)
            api.post("/v1/mining/submit", json={"hex": mined.serialize().hex()})

        real = three_way_payment()
        bob, carol, dave = real.outputs
        redirected = replace(real, outputs=(Output(MALLORY.address, bob.amount), carol, dave))
        response = api.post("/v1/tx", json={"hex": redirected.serialize().hex()})
        assert response.status_code == 400 and response.json()["error"] == "bad-signature"

        assert api.post("/v1/tx", json={"hex": real.serialize().hex()}).status_code == 200
        mallory = api.get(f"/v1/address/{encode_address(MALLORY.address, PARAMS.address_prefix)}").json()
        assert mallory["pending_in"] == "0.000000"
