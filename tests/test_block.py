from dataclasses import replace

import pytest

from talcash.core.block import (
    HEADER_SIZE,
    Block,
    block_from_bytes,
    check_block,
    compute_merkle_root,
    header_from_bytes,
)
from talcash.core.errors import ValidationError
from talcash.core.genesis import genesis_block
from talcash.core.hashing import merkle_root, sha256
from talcash.core.params import MAINNET, REGTEST, TESTNET, target_for
from talcash.core.pow import meets_target, mine, pow_hash
from talcash.crypto.address import BURN_PAYLOAD

from chainutil import TestChain, make_transfer, named_key

A, B, C, D = (sha256(bytes([i])) for i in range(4))


def leaf(x):
    return sha256(b"\x00" + x)


def node(left, right):
    return sha256(b"\x01" + left + right)


def test_merkle_matches_rfc6962_shape():
    assert merkle_root([A]) == leaf(A)
    assert merkle_root([A, B]) == node(leaf(A), leaf(B))
    assert merkle_root([A, B, C]) == node(node(leaf(A), leaf(B)), leaf(C))
    assert merkle_root([A, B, C, D]) == node(node(leaf(A), leaf(B)), node(leaf(C), leaf(D)))


def test_merkle_has_no_duplicate_last_leaf_ambiguity():
    # Bitcoin's tree gives [A, B, C] and [A, B, C, C] the same root; this one must not.
    assert merkle_root([A, B, C]) != merkle_root([A, B, C, C])
    assert merkle_root([A, B]) != merkle_root([B, A])
    # A leaf can't pose as an inner node.
    assert merkle_root([A, B]) != merkle_root([node(leaf(A), leaf(B))])


def test_regtest_genesis():
    genesis = genesis_block(REGTEST)
    assert genesis.height == 0
    assert genesis.header.prev_id == bytes(32)
    assert genesis.coinbase.address == BURN_PAYLOAD
    assert genesis.coinbase.memo == REGTEST.genesis_message
    assert meets_target(genesis.header, REGTEST.pow)
    check_block(genesis, REGTEST)


@pytest.mark.parametrize("params", [MAINNET, TESTNET])
def test_unlaunched_networks_have_no_genesis(params):
    with pytest.raises(RuntimeError, match="not been launched"):
        genesis_block(params)


def test_header_and_block_round_trip():
    chain = TestChain(REGTEST)
    block = chain.build_block()
    assert len(block.header.serialize()) == HEADER_SIZE
    assert header_from_bytes(block.header.serialize()) == block.header
    assert block_from_bytes(block.serialize()) == block


def test_mining_finds_a_valid_nonce():
    chain = TestChain(REGTEST)
    header = replace(chain.build_block().header, target=target_for(500), nonce=0)
    found = mine(header, REGTEST.pow)
    assert found is not None and meets_target(found, REGTEST.pow)
    if found.nonce > 0:
        assert not meets_target(replace(found, nonce=found.nonce - 1), REGTEST.pow)


def test_mainnet_pow_is_deterministic():
    header = genesis_block(REGTEST).header
    assert pow_hash(header, MAINNET.pow) == pow_hash(header, MAINNET.pow)
    assert pow_hash(header, MAINNET.pow) != pow_hash(header, REGTEST.pow)


def _rebuild(block: Block, transactions) -> Block:
    return Block(replace(block.header, merkle_root=compute_merkle_root(transactions)), tuple(transactions))


def test_check_block_rules():
    chain = TestChain(REGTEST)
    alice, bob = named_key("alice"), named_key("bob")
    tx = make_transfer(REGTEST, alice, 0, [(bob.address, 1)])
    block = chain.build_block([tx])
    check_block(block, REGTEST)
    coinbase = block.transactions[0]

    cases = {
        "missing-coinbase": _rebuild(block, [tx]),
        "extra-coinbase": _rebuild(block, [coinbase, coinbase]),
        "duplicate-transaction": _rebuild(block, [coinbase, tx, tx]),
        "bad-coinbase-height": _rebuild(block, [replace(coinbase, height=99), tx]),
        "bad-merkle-root": Block(block.header, (coinbase,)),
        "bad-version": Block(replace(block.header, version=2), block.transactions),
    }
    for code, bad in cases.items():
        with pytest.raises(ValidationError, match=code):
            check_block(bad, REGTEST)

    with pytest.raises(ValidationError, match="block-too-large"):
        check_block(block, replace(REGTEST, max_block_size=block.size - 1))
