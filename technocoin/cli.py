"""The `tc` command.

    tc wallet create             make a new wallet (shows your 24 words once)
    tc wallet restore            rebuild a wallet from its 24 words
    tc wallet addresses          list your addresses
    tc wallet new-address        add another address
    tc wallet show-passphrase    show your 24 words again
    tc node --mine [ADDRESS]     run a local node and mine (to your wallet's first address by default)

Global options: --network mainnet|testnet|devnet|regtest, --datadir DIR.
Try it locally: `tc --network devnet node --mine` (one block per second).
Balance, send and history arrive with the node API.
"""

import argparse
import getpass
import sys
from pathlib import Path

from . import paths
from .core.params import NETWORKS, NetworkParams
from .crypto.address import decode_address
from .node.network import load_params, reset_devnet
from .node.runner import LocalNode
from .wallet.keystore import WrongPassword
from .wallet.wallet import Wallet, WalletError

MIN_PASSWORD_LENGTH = 8


class CommandError(Exception):
    pass


def _wallet_path(args: argparse.Namespace) -> Path:
    if args.file:
        return Path(args.file)
    return paths.default_wallet_path(args.network, Path(args.datadir) if args.datadir else None)


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


def _load(args: argparse.Namespace, params: NetworkParams) -> Wallet:
    wallet = Wallet.load(_wallet_path(args))
    if wallet.params.name != params.name:
        raise WalletError(f"this is a {wallet.params.name} wallet; use --network {wallet.params.name}")
    return wallet


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


def cmd_node(args: argparse.Namespace, params: NetworkParams) -> int:
    base = Path(args.datadir) if args.datadir else None
    if args.reset:
        if params.name != "devnet":
            raise CommandError("--reset only works on devnet")
        for path in reset_devnet(base):
            print(f"removed {path}")
    params = load_params(params.name, base)
    if args.mine is None:
        raise CommandError("nothing to do yet: add --mine (the API and peer-to-peer sync come in the next steps)")
    if args.mine == "wallet":
        wallet = _load(args, params)
        miner_text = wallet.addresses[0].address
    else:
        miner_text = args.mine
    miner = decode_address(miner_text, params.address_prefix)

    node = LocalNode(params, base)
    print(node.describe(), flush=True)
    try:
        node.mine(miner, workers=args.threads, blocks=args.blocks)
    finally:
        node.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tc", description="TechnoCoin command line")
    parser.add_argument("--network", choices=sorted(NETWORKS), default="mainnet")
    parser.add_argument("--datadir", help="base folder (default: $TECHNOCOIN_HOME or ~/.technocoin)")
    commands = parser.add_subparsers(dest="command", required=True)

    wallet = commands.add_parser("wallet", help="manage your wallet")
    wallet.add_argument("--file", help="wallet file (default: <datadir>/<network>/wallet.json)")
    wallet_commands = wallet.add_subparsers(dest="wallet_command", required=True)
    for name, handler, help_text in [
        ("create", cmd_create, "make a new wallet"),
        ("restore", cmd_restore, "rebuild a wallet from its passphrase"),
        ("addresses", cmd_addresses, "list your addresses"),
        ("new-address", cmd_new_address, "add another address"),
        ("show-passphrase", cmd_show_passphrase, "show your 24 words"),
    ]:
        wallet_commands.add_parser(name, help=help_text).set_defaults(handler=handler)

    node = commands.add_parser("node", help="run a local node")
    node.add_argument("--mine", nargs="?", const="wallet", metavar="ADDRESS",
                      help="mine, paying ADDRESS (default: your wallet's first address)")
    node.add_argument("--threads", type=int, help="mining processes (default: one per CPU core)")
    node.add_argument("--blocks", type=int, help="stop after mining this many blocks")
    node.add_argument("--reset", action="store_true", help="devnet only: delete the chain and start a fresh devnet")
    node.add_argument("--file", help=argparse.SUPPRESS)  # lets _load() find the wallet the same way as `tc wallet`
    node.set_defaults(handler=cmd_node)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args, NETWORKS[args.network])
    except (CommandError, WalletError, WrongPassword, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
