# TechnoCoin

A small proof-of-work cryptocurrency written in Python: node, miner and wallet in one project,
all sharing a single implementation of the rules.

- 1 block per minute, 10 TC per block forever (14,400 TC a day)
- 6 decimals, integer amounts only
- Argon2id proof of work: CPU-friendly, no advantage for mining chips
- Difficulty adjusts smoothly on every block (ASERT)
- Daily balance snapshots committed in block headers: fast sync, pruning, balance proofs
- Daily chunks and yearly mega chunks for syncing and archives
- Ed25519 signatures, 24-word BIP39 passphrases, `tc1…` addresses

The full rules are in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Layout

```
technocoin/core/     consensus rules: encoding, transactions, blocks, PoW, difficulty, state
technocoin/crypto/   keys, passphrases, addresses
technocoin/wallet/   encrypted wallet file, node client
technocoin/node/     chain (SQLite, branches, finality), mempool, API server, peer-to-peer, built-in miner
technocoin/miner/    multi-core Argon2id miner
technocoin/devnet.py `tc devnet`: several local nodes at once
technocoin/cli.py    the `tc` command
tests/               test suite
docs/PROTOCOL.md     protocol specification
```

Coming next: hardening, chunk files for fast sync, a public testnet, then launch.

## Try it: a local devnet

Devnet is TechnoCoin on your own computer at 60x speed: one block per second, same rules and
mining as mainnet.

```
python -m technocoin --network devnet wallet create
python -m technocoin --network devnet node --mine
```

The node mines to your wallet's first address and prints every block: difficulty climbing as it
adapts to your CPU, the chain settling at about one block per second, rewards unlocking after 100
blocks. Stop it with Ctrl+C and start it again to continue the same chain; add `--reset` to throw
the devnet away and start a fresh one.

`--threads N` sets how many processes mine. The default is about one per physical core. More
rarely helps: every hash needs 4 MiB of memory, so extra workers just compete for memory
bandwidth (that's what keeps big machines from dominating).

While the node runs, in a second terminal:

```
python -m technocoin --network devnet wallet balance
python -m technocoin --network devnet wallet send td1... 12.5 --memo "thanks" --wait
python -m technocoin --network devnet wallet send td1... 3 td1... 1.25    # several receivers, one payment
python -m technocoin --network devnet wallet history
```

## Several nodes on one computer

```
python -m technocoin devnet --nodes 3 --miners 2
```

Starts three devnet nodes as separate processes (ports 64187, 64188, 64189), all connected, two of
them mining against each other; every line of output is tagged with its node. Miners pay your
devnet wallet if you have one. Wallet commands talk to the first node by default; use
`wallet --node http://127.0.0.1:64189 ...` for another. `--reset` starts a brand-new devnet.

To connect nodes by hand: `tc node --peer ws://HOST:PORT/v1/p2p` (repeatable). A new devnet node
started with `--peer` joins that devnet instead of creating its own.

## Wallet

```
tc wallet create             new wallet; shows your 24 words once
tc wallet restore            from your 24 words
tc wallet addresses          your addresses
tc wallet new-address        add another address
tc wallet show-passphrase    your 24 words again
tc wallet balance            available, incoming, outgoing, unlocking
tc wallet send ADDRESS AMOUNT [ADDRESS AMOUNT ...] [--fee X] [--from N] [--memo TEXT] [--wait] [--yes]
tc wallet history
```

`tc` is `python -m technocoin` until you `pip install -e .`. Put `--network testnet|devnet`
before `wallet` for other networks. Wallets live in `~/.technocoin/<network>/wallet.json` (change
with `--datadir` or `TECHNOCOIN_HOME`). Commands that need a node talk to the one on this computer;
use `wallet --node http://host:port ...` for another.

## Node API

`tc node` serves an HTTP + WebSocket API on the network's port (64187 on devnet), on this computer
only unless you pass `--host`. Interactive documentation: http://127.0.0.1:64187/docs. The endpoints
are listed in [docs/PROTOCOL.md](docs/PROTOCOL.md), section 15.

## Development

Requires Python 3.11+.

```
pip install -e ".[dev]"
python -m pytest
```
