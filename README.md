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
tests/               test suite
docs/PROTOCOL.md     protocol specification
```

Coming next: `technocoin/node` (storage, chain, mempool, API, P2P, chunks),
`technocoin/miner`, `technocoin/wallet` (CLI).

## Development

Requires Python 3.11+.

```
pip install -e ".[dev]"
python -m pytest
```
