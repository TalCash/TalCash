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
technocoin/wallet/   encrypted wallet file
technocoin/node/     SQLite storage and chain manager (branches, reorganisation, finality)
technocoin/cli.py    the `tc` command
tests/               test suite
docs/PROTOCOL.md     protocol specification
```

Coming next: mempool and miner, then the node's HTTP/WebSocket API, peer-to-peer
sync with chunks, and launch.

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

## Wallet

```
python -m technocoin wallet create          # new wallet; shows your 24 words once
python -m technocoin wallet restore         # from your 24 words
python -m technocoin wallet addresses
python -m technocoin wallet new-address
python -m technocoin wallet show-passphrase
```

Add `--network testnet` or `--network regtest` before `wallet` for other networks.
Wallets live in `~/.technocoin/<network>/wallet.json` (change with `--datadir` or
`TECHNOCOIN_HOME`). After `pip install -e .` the command is simply `tc`.

## Development

Requires Python 3.11+.

```
pip install -e ".[dev]"
python -m pytest
```
