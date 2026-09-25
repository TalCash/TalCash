# TalCash

A small proof-of-work cryptocurrency written in Python: node, miner and wallet in one project,
all sharing a single implementation of the rules. Website: [talcash.com](https://talcash.com)

> **Status: in development.** No mainnet has launched and no TalCash coin or token is for sale
> anywhere; anything claiming otherwise is not this project. Found a security problem? See
> [SECURITY.md](SECURITY.md).

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
talcash/core/      consensus rules: encoding, transactions, blocks, PoW, difficulty, state
talcash/crypto/    keys, passphrases, addresses
talcash/wallet/    encrypted wallet file, node client
talcash/node/      block files, chain (SQLite index, branches, finality), mempool, API, peer-to-peer
talcash/miner/     multi-core Argon2id miner
talcash/devnet.py  `tc devnet`: several local nodes at once
talcash/cli.py     the `tc` command
tests/             test suite
docs/PROTOCOL.md   protocol specification
```

Coming next: a public testnet, a block explorer, then launch.

## Try it: a local devnet

Devnet is TalCash on your own computer at 60x speed: one block per second, same rules and
mining as mainnet.

```
python -m talcash --network devnet wallet create
python -m talcash --network devnet node --mine
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
python -m talcash --network devnet wallet balance
python -m talcash --network devnet wallet send td1... 12.5 --memo "thanks" --wait
python -m talcash --network devnet wallet send td1... 3 td1... 1.25    # several receivers, one payment
python -m talcash --network devnet wallet history
```

## Several nodes on one computer

```
python -m talcash devnet --nodes 3 --miners 2
```

Starts three devnet nodes as separate processes (ports 64187, 64188, 64189), all connected, two of
them mining against each other; every line of output is tagged with its node. Miners pay your
devnet wallet if you have one. Wallet commands talk to the first node by default; use
`wallet --node http://127.0.0.1:64189 ...` for another. `--reset` starts a brand-new devnet.

To connect nodes by hand: `tc node --peer ws://HOST:PORT/v1/p2p` (repeatable). A new devnet node
started with `--peer` joins that devnet instead of creating its own.

## Block files

Every node keeps the chain as files: `~/.talcash/<network>/blocks/chunks/*.chunk` (one file per
sealed day, compressed) and `blocks/recent/*.block` (newer blocks, one file each). The database next
to them is only an index plus balances and can always be rebuilt:

```
python -m talcash --network devnet node --reindex      # rebuild the database from the files
python -m talcash read <file>.chunk [--blocks]         # check a file and show it as JSON
```

Copy the `blocks` folder to another machine and `--reindex` there gives it the whole chain.

A node that joins late downloads the sealed day files from its peers and imports a whole day at
a time, then fetches only the newest blocks one by one. Nodes remember the peers they reached
(`peers.json`), so after a restart no `--peer` is needed.

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

`tc` is `python -m talcash` until you `pip install -e .`. Put `--network testnet|devnet`
before `wallet` for other networks. Wallets live in `~/.talcash/<network>/wallet.json` (change
with `--datadir` or `TALCASH_HOME`). Commands that need a node talk to the one on this computer;
use `wallet --node http://host:port ...` for another.

## Node API

`tc node` serves an HTTP + WebSocket API on the network's port (64187 on devnet), on this computer
only unless you pass `--host`. Interactive documentation: http://127.0.0.1:64187/docs. The endpoints
are listed in [docs/PROTOCOL.md](docs/PROTOCOL.md), section 15.

With `--host 0.0.0.0` (so other machines' nodes can connect) the API goes into public mode: other
computers get a rate-limited API without the mining endpoints. `--trust IP` gives one address full
access, e.g. a separate mining computer on your network.

## Safety against misbehaving peers

- Headers first: a node checks every header of a chain (links, timestamps, difficulty, proof of
  work) and only downloads the blocks of a chain that really has more work than its own.
- Every peer has a message budget; a flooding peer is read more slowly instead of served faster.
- A peer that sends anything an honest node never would (invalid blocks or headers, forged
  payments, damaged day files, garbage) is disconnected and banned for an hour.
- Everything that reads outside data is fuzz-tested (`tests/test_fuzz.py`; run it longer with
  `TC_FUZZ_ROUNDS=100`).

## Development

Requires Python 3.11+.

```
pip install -e ".[dev]"
python -m pytest
```

## License

MIT, see [LICENSE](LICENSE).
