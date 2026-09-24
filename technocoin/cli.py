"""The `tc` command.

    tc wallet create                 make a new wallet (shows your 24 words once)
    tc wallet restore                rebuild a wallet from its 24 words
    tc wallet addresses              list your addresses
    tc wallet new-address            add another address
    tc wallet show-passphrase        show your 24 words again
    tc wallet balance                balances (asks the node)
    tc wallet send ADDRESS AMOUNT    send coins
    tc wallet history                recent transactions
    tc node [--mine [ADDRESS]]       run a node, optionally mining (to your wallet by default)

Global options: --network mainnet|testnet|devnet|regtest, --datadir DIR.
Wallet commands that need a node use --node URL (default: this computer).
Try it locally: `tc --network devnet node --mine` (one block per second).
"""

import argparse
import getpass
import sys
import time
from pathlib import Path

from . import paths
from .core.amounts import format_amount, parse_amount
from .core.params import NETWORKS, NetworkParams
from .core.tx import MAX_MEMO_SIZE, Output, Transfer
from .crypto.address import decode_address
from .node.network import load_params, reset_devnet
from .node.server import run_node
from .wallet.client import NodeClient, NodeError
from .wallet.keystore import WrongPassword
from .wallet.wallet import Wallet, WalletError

MIN_PASSWORD_LENGTH = 8


class CommandError(Exception):
    pass


# --- helpers -----------------------------------------------------------------

def _wallet_path(args: argparse.Namespace) -> Path:
    if args.file:
        return Path(args.file)
    return paths.default_wallet_path(args.network, Path(args.datadir) if args.datadir else None)


def _load(args: argparse.Namespace, params: NetworkParams) -> Wallet:
    wallet = Wallet.load(_wallet_path(args))
    if wallet.params.name != params.name:
        raise WalletError(f"this is a {wallet.params.name} wallet; use --network {wallet.params.name}")
    return wallet


def _client(args: argparse.Namespace, params: NetworkParams) -> tuple[NodeClient, dict]:
    client = NodeClient(args.node or f"http://127.0.0.1:{params.default_port}")
    status = client.status()
    if status["network"] != params.name:
        raise CommandError(f"the node at {client.url} runs {status['network']}, not {params.name}")
    return client, status


def _ask_new_password() -> str:
    while True:
        password = getpass.getpass("Choose a wallet password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if getpass.getpass("Repeat the password: ") != password:
            print("The passwords don't match, try again.")
            continue
        return password


def _print_passphrase(phrase: str) -> None:
    words = phrase.split()
    rows = (len(words) + 2) // 3
    for row in range(rows):
        cells = [f"{i + 1:>2}. {words[i]:<10}" for i in range(row, len(words), rows)]
        print("   " + "  ".join(cells))


def _tc(text: str) -> str:
    """The API's '12.500000' / '-1.000146' shown as '12.5' / '-1.000146'."""
    sign = "-" if text.startswith("-") else ""
    return sign + format_amount(parse_amount(text.lstrip("-")))


# --- wallet commands ---------------------------------------------------------

def cmd_create(args: argparse.Namespace, params: NetworkParams) -> int:
    path = _wallet_path(args)
    if path.exists():
        raise WalletError(f"a wallet already exists at {path}")
    password = _ask_new_password()
    wallet, phrase = Wallet.create(path, params, password)
    print()
    print("Your wallet passphrase. Write these words on paper, in order, and keep them safe:")
    print()
    _print_passphrase(phrase)
    print()
    print("Anyone who sees these words can take your coins. If you lose them AND forget")
    print("your password, your coins are gone for good. They will not be shown again")
    print("unless you run `tc wallet show-passphrase`.")
    print()
    print(f"Address:  {wallet.addresses[0].address}")
    print(f"Saved to: {path}")
    return 0


def cmd_restore(args: argparse.Namespace, params: NetworkParams) -> int:
    path = _wallet_path(args)
    if path.exists():
        raise WalletError(f"a wallet already exists at {path}")
    phrase = getpass.getpass("Type your passphrase words, separated by spaces (hidden): ")
    password = _ask_new_password()
    wallet = Wallet.restore(path, params, phrase, password)
    print(f"Wallet restored. Address: {wallet.addresses[0].address}")
    print("If you used more addresses before, run `tc wallet new-address` until they appear.")
    return 0


def cmd_addresses(args: argparse.Namespace, params: NetworkParams) -> int:
    for entry in _load(args, params).addresses:
        print(f"#{entry.index}  {entry.address}")
    return 0


def cmd_new_address(args: argparse.Namespace, params: NetworkParams) -> int:
    wallet = _load(args, params)
    entry = wallet.new_address(getpass.getpass("Wallet password: "))
    print(f"#{entry.index}  {entry.address}")
    return 0


def cmd_show_passphrase(args: argparse.Namespace, params: NetworkParams) -> int:
    wallet = _load(args, params)
    phrase = wallet.reveal_passphrase(getpass.getpass("Wallet password: "))
    _print_passphrase(phrase)
    return 0


def cmd_balance(args: argparse.Namespace, params: NetworkParams) -> int:
    wallet = _load(args, params)
    client, status = _client(args, params)
    totals = {"available": 0, "pending_in": 0, "pending_out": 0, "immature": 0}
    print(f"{'':4} {'address':<40} {'available':>16} {'incoming':>14} {'outgoing':>14} {'unlocking':>14}")
    for entry in wallet.addresses:
        info = client.address(entry.address)
        for key in totals:
            totals[key] += parse_amount(info[key])
        print(f"#{entry.index:<3} {entry.address:<40} {_tc(info['available']):>16} {_tc(info['pending_in']):>14} "
              f"{_tc(info['pending_out']):>14} {_tc(info['immature']):>14}")
    print()
    print(f"Available now: {format_amount(totals['available'])} TC")
    if totals["pending_in"] or totals["pending_out"]:
        print(f"Waiting to be mined: +{format_amount(totals['pending_in'])} in, "
              f"-{format_amount(totals['pending_out'])} out")
    if totals["immature"]:
        print(f"Mining rewards unlocking over the next {status['coinbase_maturity']} blocks: "
              f"{format_amount(totals['immature'])} TC")
    return 0


def cmd_send(args: argparse.Namespace, params: NetworkParams) -> int:
    wallet = _load(args, params)
    sender = next((a for a in wallet.addresses if a.index == args.from_index), None)
    if sender is None:
        raise CommandError(f"the wallet has no address #{args.from_index}")
    receiver = decode_address(args.to, params.address_prefix)
    amount = parse_amount(args.amount)
    if amount == 0:
        raise CommandError("the amount must be more than 0")
    memo = (args.memo or "").encode("utf-8")
    if len(memo) > MAX_MEMO_SIZE:
        raise CommandError(f"the memo can be at most {MAX_MEMO_SIZE} bytes")

    client, status = _client(args, params)
    info = client.address(sender.address)
    size = Transfer(params.network_id, bytes(32), 0, 0, (Output(receiver, amount),), memo).size
    fee = parse_amount(args.fee) if args.fee else parse_amount(status["min_fee_per_byte"]) * size
    available = parse_amount(info["available"])
    if amount + fee > available:
        raise CommandError(f"not enough coins: {format_amount(available)} TC available at #{sender.index}, "
                           f"this needs {format_amount(amount + fee)} TC")

    print(f"From:   {sender.address} (#{sender.index})")
    print(f"To:     {args.to}")
    print(f"Amount: {format_amount(amount)} TC")
    print(f"Fee:    {format_amount(fee)} TC")
    if memo:
        print(f"Memo:   {args.memo}")
    if not args.yes and input("Send? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 1
    tx = wallet.sign_transfer(getpass.getpass("Wallet password: "), index=sender.index, nonce=info["next_nonce"],
                              fee=fee, outputs=[(args.to, amount)], memo=memo)
    txid = client.submit(tx)["txid"]
    print(f"Sent. Transaction {txid}")
    if args.wait:
        deadline = time.monotonic() + max(60, 20 * status["target_spacing"])
        while time.monotonic() < deadline:
            found = client.transaction(txid)
            if found and found["status"] == "confirmed":
                print(f"Confirmed in block #{found['height']}.")
                return 0
            time.sleep(1)
        print("Not mined yet; check later with `tc wallet history`.")
    return 0


def cmd_history(args: argparse.Namespace, params: NetworkParams) -> int:
    wallet = _load(args, params)
    client, _ = _client(args, params)
    for entry in wallet.addresses:
        print(f"#{entry.index} {entry.address}")
        items = client.history(entry.address, args.limit)
        if not items:
            print("   (nothing yet)")
        for item in items:
            if item["status"] == "pending":
                when = "waiting to be mined"
            else:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["time"]))
                when = f"{stamp}  block #{item['height']}"
            if item["kind"] == "mined":
                other = "block reward"
            elif item["kind"] == "received":
                other = f"from {item['from']}"
            else:
                other = "to " + ", ".join(o["address"] for o in item["outputs"])
            print(f"   {_tc(item['amount']):>16} TC  {item['kind']:<8} {other}  ({when})")
        print()
    return 0


# --- node ------------------------------------------------------------------

def cmd_node(args: argparse.Namespace, params: NetworkParams) -> int:
    base = Path(args.datadir) if args.datadir else None
    if args.reset:
        if params.name != "devnet":
            raise CommandError("--reset only works on devnet")
        for path in reset_devnet(base):
            print(f"removed {path}")
    params = load_params(params.name, base)
    miner = None
    if args.mine is not None:
        text = _load(args, params).addresses[0].address if args.mine == "wallet" else args.mine
        miner = decode_address(text, params.address_prefix)
    run_node(params, base, host=args.host, port=args.port, miner=miner, workers=args.threads,
             blocks=args.blocks, min_fee_per_byte=args.min_fee)
    return 0


# --- wiring ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tc", description="TechnoCoin command line")
    parser.add_argument("--network", choices=sorted(NETWORKS), default="mainnet")
    parser.add_argument("--datadir", help="base folder (default: $TECHNOCOIN_HOME or ~/.technocoin)")
    commands = parser.add_subparsers(dest="command", required=True)

    wallet = commands.add_parser("wallet", help="manage your wallet")
    wallet.add_argument("--file", help="wallet file (default: <datadir>/<network>/wallet.json)")
    wallet.add_argument("--node", help="node API address (default: http://127.0.0.1:<network port>)")
    wallet_commands = wallet.add_subparsers(dest="wallet_command", required=True)
    for name, handler, help_text in [
        ("create", cmd_create, "make a new wallet"),
        ("restore", cmd_restore, "rebuild a wallet from its passphrase"),
        ("addresses", cmd_addresses, "list your addresses"),
        ("new-address", cmd_new_address, "add another address"),
        ("show-passphrase", cmd_show_passphrase, "show your 24 words"),
        ("balance", cmd_balance, "show balances (asks the node)"),
    ]:
        wallet_commands.add_parser(name, help=help_text).set_defaults(handler=handler)

    send = wallet_commands.add_parser("send", help="send coins")
    send.add_argument("to", help="receiving address")
    send.add_argument("amount", help="amount in TC, e.g. 12.5")
    send.add_argument("--fee", help="fee in TC (default: the node's minimum)")
    send.add_argument("--from", dest="from_index", type=int, default=0, help="send from address #N (default 0)")
    send.add_argument("--memo", help="a short note stored with the payment (public)")
    send.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    send.add_argument("--wait", action="store_true", help="wait until the payment is in a block")
    send.set_defaults(handler=cmd_send)

    history = wallet_commands.add_parser("history", help="recent transactions")
    history.add_argument("--limit", type=int, default=20)
    history.set_defaults(handler=cmd_history)

    node = commands.add_parser("node", help="run a node")
    node.add_argument("--mine", nargs="?", const="wallet", metavar="ADDRESS",
                      help="mine, paying ADDRESS (default: your wallet's first address)")
    node.add_argument("--threads", type=int, help="mining processes (default: about one per physical core)")
    node.add_argument("--blocks", type=int, help="stop after mining this many blocks")
    node.add_argument("--host", default="127.0.0.1", help="API address to listen on (default: this computer only)")
    node.add_argument("--port", type=int, help="API port (default: the network's port)")
    node.add_argument("--min-fee", type=int, default=1, help="smallest fee this node relays, in base units per byte")
    node.add_argument("--reset", action="store_true", help="devnet only: delete the chain and start a fresh devnet")
    node.add_argument("--file", help=argparse.SUPPRESS)  # lets _load() find the wallet the same way as `tc wallet`
    node.set_defaults(handler=cmd_node)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args, NETWORKS[args.network])
    except NodeError as error:
        print(f"error from node: {error}", file=sys.stderr)
        return 1
    except (CommandError, WalletError, WrongPassword, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
