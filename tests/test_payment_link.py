"""Payment links (talcash:...), checked against the shared test cases in docs/payment-links.json."""

import json
from pathlib import Path

import pytest

from talcash.cli import main
from talcash.core.params import MAINNET, REGTEST, TESTNET
from talcash.core.tx import MAX_MEMO_SIZE
from talcash.wallet.keystore import INSECURE_FAST
from talcash.wallet.payment_link import (MAX_LABEL_LENGTH, MAX_LINK_LENGTH, PaymentLinkError, PaymentRequest,
                                         is_link, parse_link)
from talcash.wallet.wallet import Wallet
from test_wallet_live import PASSWORD, mine_to, node_url  # noqa: F401  (node_url is a fixture)

CASES = json.loads((Path(__file__).parent.parent / "docs" / "payment-links.json").read_text(encoding="utf-8"))
ADDRESS = "tt1BfvB8KSm1VTToN85j3Po4pWZcct6R3wes"


@pytest.mark.parametrize("case", CASES["valid"], ids=lambda case: case["link"][:70])
def test_valid_links(case):
    request = parse_link(case["link"])
    assert (request.network, request.address, request.amount, request.memo, request.label) == \
        (case["network"], case["address"], case["amount"], case["memo"], case["label"])
    assert parse_link(request.to_link()) == request  # and it writes back to a link that means the same


@pytest.mark.parametrize("case", CASES["invalid"], ids=lambda case: case["why"])
def test_invalid_links(case):
    with pytest.raises(PaymentLinkError):
        parse_link(case["link"])


def test_the_link_must_be_for_the_wallets_network():
    assert parse_link(f"talcash:{ADDRESS}", TESTNET).network == "testnet"
    with pytest.raises(PaymentLinkError, match="for testnet, not mainnet"):
        parse_link(f"talcash:{ADDRESS}?amount=1", MAINNET)


def test_limits():
    longest_memo = "é" * (MAX_MEMO_SIZE // 2) + "a"  # 255 bytes
    assert parse_link(PaymentRequest("testnet", ADDRESS, memo=longest_memo).to_link()).memo == longest_memo
    with pytest.raises(PaymentLinkError, match="memo"):
        PaymentRequest("testnet", ADDRESS, memo=longest_memo + "a").to_link()
    assert parse_link(f"talcash:{ADDRESS}?label={'a' * MAX_LABEL_LENGTH}").label == "a" * MAX_LABEL_LENGTH
    with pytest.raises(PaymentLinkError, match="label"):
        parse_link(f"talcash:{ADDRESS}?label={'a' * (MAX_LABEL_LENGTH + 1)}")
    padding = "&x=" + "a" * 2000
    link = f"talcash:{ADDRESS}?amount=1{padding}"[:MAX_LINK_LENGTH]
    assert parse_link(link).amount == 1_000_000
    with pytest.raises(PaymentLinkError, match="longer than"):
        parse_link(link + "a")


def test_writing_links():
    request = PaymentRequest("testnet", ADDRESS, 1, "100% & more?", "Tal's shop")
    link = request.to_link()
    assert link == f"talcash:{ADDRESS}?amount=0.000001&memo=100%25%20%26%20more%3F&label=Tal%27s%20shop"
    assert parse_link(link) == request and is_link(link) and not is_link(ADDRESS)
    with pytest.raises(PaymentLinkError):  # nothing unreadable ever gets written
        PaymentRequest("testnet", ADDRESS, memo="line\nbreak").to_link()


def cli(tmp_path, *words):
    return main(["--network", "regtest", "--datadir", str(tmp_path), "wallet", *words])


def test_request_then_pay_the_link(tmp_path, node_url, monkeypatch, capsys):  # noqa: F811
    shop, _ = Wallet.create(tmp_path / "shop.json", REGTEST, PASSWORD, strength=INSECURE_FAST)
    Wallet.create(tmp_path / "regtest" / "wallet.json", REGTEST, PASSWORD, strength=INSECURE_FAST)
    buyer = Wallet.load(tmp_path / "regtest" / "wallet.json")
    mine_to(node_url, buyer.addresses[0].address, 4)  # 3 blocks to unlock the first reward
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)

    assert cli(tmp_path, "--file", str(tmp_path / "shop.json"), "request", "2.5", "--memo", "order #17 ☕",
               "--label", "Tal's shop") == 0
    link = capsys.readouterr().out.strip()
    assert link.startswith(f"talcash:{shop.addresses[0].address}?amount=2.5&memo=order%20%2317")

    assert cli(tmp_path, "--node", node_url, "send", link, "--yes") == 0
    out = capsys.readouterr().out
    assert "Name:   Tal's shop" in out and "Memo:   order #17 ☕" in out and "Sent. Transaction" in out
    mine_to(node_url, buyer.addresses[0].address, 1)
    assert cli(tmp_path, "--file", str(tmp_path / "shop.json"), "--node", node_url, "history") == 0
    assert "2.5 TC  received" in capsys.readouterr().out

    # A link without an amount needs one; a link with one takes no other; the memo can't be replaced.
    open_link = f"talcash:{shop.addresses[0].address}"
    assert cli(tmp_path, "--node", node_url, "send", open_link, "--yes") == 1
    assert "has no amount" in capsys.readouterr().err
    assert cli(tmp_path, "--node", node_url, "send", link, "3", "--yes") == 1
    assert "already asks for 2.5 TC" in capsys.readouterr().err
    assert cli(tmp_path, "--node", node_url, "send", link, "--memo", "other", "--yes") == 1
    assert "already sets the memo" in capsys.readouterr().err
    assert cli(tmp_path, "--node", node_url, "send", open_link, "0.5", "--memo", "tip", "--yes") == 0
    capsys.readouterr()
    assert cli(tmp_path, "--node", node_url, "send", link.replace("talcash:tr1", "talcash:tt1"), "--yes") == 1
    assert "payment link" in capsys.readouterr().err  # a testnet or damaged link, refused before anything else


def test_request_needs_a_real_address_and_amount(tmp_path, capsys):
    Wallet.create(tmp_path / "regtest" / "wallet.json", REGTEST, PASSWORD, strength=INSECURE_FAST)
    assert cli(tmp_path, "request") == 0
    assert capsys.readouterr().out.strip().startswith("talcash:tr1")
    assert cli(tmp_path, "request", "0") == 1
    assert cli(tmp_path, "request", "--address", "5") == 1
    assert cli(tmp_path, "request", "--memo", "bad‮one") == 1
    assert "control character" in capsys.readouterr().err
