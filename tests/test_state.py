"""The state rules, including a regression test for every money bug in the old PHP node."""

from dataclasses import replace

import pytest

from talcash.core.amounts import COIN
from talcash.core.block import Block, compute_merkle_root
from talcash.core.errors import ValidationError
from talcash.core.params import MAINNET, REGTEST, target_for
from talcash.core.pow import meets_target
from talcash.core.state import apply_block, check_not_in_future

from chainutil import TestChain, make_transfer, named_key

MATURITY = 3
PARAMS = replace(REGTEST, coinbase_maturity=MATURITY)
REWARD = PARAMS.block_reward

ALICE, BOB, CAROL, MINER = named_key("alice"), named_key("bob"), named_key("carol"), named_key("miner")


def funded_chain() -> TestChain:
    """Heights 1-2 mined by Alice (matured: 20 TC), heights 3-5 by MINER (not yet matured)."""
    chain = TestChain(PARAMS)
    chain.mine_blocks(2, miner=ALICE.address)
    chain.mine_blocks(MATURITY, miner=MINER.address)
    assert chain.balance(ALICE.address) == 2 * REWARD
    return chain


@pytest.fixture
def chain() -> TestChain:
    return funded_chain()


def assert_rejected(chain: TestChain, block: Block, code: str) -> None:
    before = dict(chain.state.accounts)
    with pytest.raises(ValidationError, match=code):
        chain.add(block)
    assert chain.state.accounts == before  # a rejected block changes nothing


def test_rewards_are_spendable_only_after_maturity():
    chain = TestChain(PARAMS)
    chain.add(chain.build_block(miner=ALICE.address))
    for _ in range(MATURITY - 1):
        chain.mine_blocks(1)
        assert chain.balance(ALICE.address) == 0
    chain.mine_blocks(1)
    assert chain.balance(ALICE.address) == REWARD


def test_genesis_reward_goes_to_the_burn_address():
    chain = TestChain(PARAMS)
    chain.mine_blocks(MATURITY)
    assert chain.balance(bytes(21)) == REWARD


def test_rewards_are_paid_exactly_once():
    """Old bug: every new block re-credited every past transaction."""
    chain = TestChain(PARAMS)
    chain.mine_blocks(10, miner=ALICE.address)
    chain.mine_blocks(50)
    assert chain.balance(ALICE.address) == 10 * REWARD


def test_transfer_moves_money_and_fees_reach_the_miner(chain):
    tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 3 * COIN), (CAROL.address, 1)], fee=500)
    changes = chain.add(chain.build_block([tx], miner=MINER.address))
    assert changes.fees == 500
    assert chain.balance(ALICE.address) == 2 * REWARD - 3 * COIN - 1 - 500
    assert chain.balance(BOB.address) == 3 * COIN
    assert chain.balance(CAROL.address) == 1
    assert chain.state.get_account(ALICE.address).nonce == 1
    miner_before = chain.balance(MINER.address)
    chain.mine_blocks(MATURITY, miner=MINER.address)
    # Heights 7-9 mature the coinbases of heights 4, 5 and 6; height 6 carried the 500 fee.
    assert chain.balance(MINER.address) - miner_before == 3 * REWARD + 500


def test_double_spend_in_one_block_is_rejected(chain):
    """Old bug: the balance check ignored pending payments, so the same coins could be spent many times."""
    first = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 2 * REWARD)])
    same_nonce = make_transfer(PARAMS, ALICE, 0, [(CAROL.address, 2 * REWARD)])
    assert_rejected(chain, chain.build_block([first, same_nonce]), "bad-nonce")
    next_nonce = make_transfer(PARAMS, ALICE, 1, [(CAROL.address, 1)])
    assert_rejected(chain, chain.build_block([first, next_nonce]), "insufficient-funds")


def test_replaying_a_transfer_is_rejected(chain):
    tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, COIN)])
    chain.add(chain.build_block([tx]))
    assert_rejected(chain, chain.build_block([tx]), "bad-nonce")


def test_nonces_must_be_in_order(chain):
    assert_rejected(chain, chain.build_block([make_transfer(PARAMS, ALICE, 1, [(BOB.address, 1)])]), "bad-nonce")


def test_insufficient_funds(chain):
    tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 2 * REWARD)], fee=1)
    assert_rejected(chain, chain.build_block([tx]), "insufficient-funds")
    broke = make_transfer(PARAMS, BOB, 0, [(ALICE.address, 1)])
    assert_rejected(chain, chain.build_block([broke]), "insufficient-funds")


def test_nobody_can_debit_someone_else(chain):
    """Old bug: negative receiver amounts let a sender drain any address."""
    fake = make_transfer(PARAMS, BOB, 0, [(BOB.address, COIN)])
    forged = replace(fake, sender_public_key=ALICE.public_key)  # claims to be Alice, signed by Bob
    assert_rejected(chain, chain.build_block([forged]), "bad-signature")


def test_coinbase_must_claim_exactly_reward_plus_fees(chain):
    """Old bug: fees could never be collected (always burned)."""
    tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, COIN)], fee=700)
    assert_rejected(chain, chain.build_block([tx], coinbase_amount=REWARD), "bad-coinbase-amount")
    assert_rejected(chain, chain.build_block([tx], coinbase_amount=REWARD + 701), "bad-coinbase-amount")
    chain.add(chain.build_block([tx], coinbase_amount=REWARD + 700))


def test_transactions_from_another_network_are_rejected(chain):
    tx = make_transfer(MAINNET, ALICE, 0, [(BOB.address, COIN)])
    assert_rejected(chain, chain.build_block([tx]), "wrong-network")


def test_sending_to_yourself(chain):
    tx = make_transfer(PARAMS, ALICE, 0, [(ALICE.address, COIN)], fee=10)
    chain.add(chain.build_block([tx]))
    assert chain.balance(ALICE.address) == 2 * REWARD - 10


def test_chained_transfers_in_one_block(chain):
    pay_bob = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 5 * COIN)])
    bob_pays_carol = make_transfer(PARAMS, BOB, 0, [(CAROL.address, 2 * COIN)])
    chain.add(chain.build_block([pay_bob, bob_pays_carol]))
    assert chain.balance(BOB.address) == 3 * COIN
    assert chain.balance(CAROL.address) == 2 * COIN
    # Order matters: Bob can't spend before he's paid.
    other = funded_chain()
    assert_rejected(other, other.build_block([bob_pays_carol, pay_bob]), "insufficient-funds")


def test_revert_restores_the_exact_previous_state(chain):
    before = dict(chain.state.accounts)
    tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 4 * COIN)], fee=3)
    changes = chain.add(chain.build_block([tx]))
    assert chain.state.accounts != before
    chain.state.revert(changes)
    assert chain.state.accounts == before


def test_header_rules(chain):
    ctx = chain.context()
    block = chain.build_block()
    header = block.header

    def with_header(**changes):
        return Block(replace(header, **changes), block.transactions)

    assert_rejected(chain, with_header(prev_id=bytes(32)), "bad-prev-id")
    assert_rejected(chain, with_header(timestamp=ctx.median_time_past), "time-too-old")
    assert_rejected(chain, with_header(target=header.target - 1), "bad-target")

    wrong_height = chain.build_block()
    wrong_height = Block(replace(wrong_height.header, height=header.height + 1), wrong_height.transactions)
    assert_rejected(chain, wrong_height, "bad-height")

    nonce = 0
    while meets_target(replace(header, nonce=nonce), PARAMS.pow):
        nonce += 1
    assert_rejected(chain, with_header(nonce=nonce), "bad-pow")


def test_easier_target_than_expected_is_rejected(chain):
    block = chain.build_block(target=target_for(1))
    assert_rejected(chain, block, "bad-target")


def test_block_must_not_be_from_the_future(chain):
    header = chain.build_block().header
    check_not_in_future(header, header.timestamp - PARAMS.max_future_drift, PARAMS)
    with pytest.raises(ValidationError, match="time-too-new"):
        check_not_in_future(header, header.timestamp - PARAMS.max_future_drift - 1, PARAMS)


def test_apply_block_does_not_touch_state(chain):
    tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, COIN)])
    before = dict(chain.state.accounts)
    apply_block(chain.build_block([tx]), chain.context(), chain.state, PARAMS)
    assert chain.state.accounts == before


def test_merkle_root_commits_to_signatures(chain):
    """Swapping in a tampered transaction changes the merkle root, so the header no longer matches."""
    tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, COIN)])
    block = chain.build_block([tx])
    tampered = replace(tx, signature=bytes(64))
    assert compute_merkle_root((block.transactions[0], tampered)) != block.header.merkle_root
    assert_rejected(chain, Block(block.header, (block.transactions[0], tampered)), "bad-merkle-root")
