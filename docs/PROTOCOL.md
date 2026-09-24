# TechnoCoin Protocol

Version 1 (draft). This document is the source of truth for the consensus rules:
anything that decides whether a transaction or block is valid. The reference
implementation is `technocoin/core` (rules) and `technocoin/crypto` (keys and
addresses); if the two ever disagree, that is a bug to fix in one of them.

Status of each part:

| Part | Status |
|---|---|
| Amounts, encoding, hashing, addresses, transactions, blocks, PoW, difficulty, state, balance snapshots | Implemented and tested |
| Genesis | Implemented; mainnet/testnet genesis gets mined at launch |
| Chain selection, reorganisation, finality | Implemented in `technocoin/node/chain.py` and tested |
| Mempool policy, block templates, miner | Implemented and tested |
| Node API (HTTP + WebSocket) | Implemented and tested |
| Peer-to-peer: handshake, spreading blocks and transfers, catching up | Implemented and tested with several real nodes |
| Block files and sealed chunk files, rebuild from files, reader | Implemented and tested (section 12) |
| Chunk download during sync, mega chunks, fast sync, pruning | Planned (section 12) |

---

## 1. Amounts

- All amounts are integers counted in **base units**. `1 TC = 1,000,000 base units` (6 decimals).
- No amount or balance may exceed `MAX_AMOUNT = 2^63 − 1`.
- Floats are never used for money. Wallets parse "12.5" into `12_500_000` with string arithmetic.

## 2. Encoding and hashing

- Integers are fixed-width, unsigned, **big-endian** (`u8`, `u32`, `u64`, `u256`).
- `var8` = one length byte followed by that many bytes (max 255).
- Every object has exactly one valid encoding. Decoders reject unknown versions,
  unknown types, truncated data and trailing bytes.
- `H(x)` = SHA-256.

## 3. Keys and addresses

- Signatures: **Ed25519** (libsodium). Public keys are 32 bytes, signatures 64 bytes.
- **Address payload** (21 bytes, used inside transactions):
  `0x00 || H(public_key)[0:20]`. The first byte is the address version; only `0x00` is valid today.
- **Text address**: `prefix || base58(payload || checksum)` with
  `checksum = H(H(prefix || payload))[0:4]`. Prefixes: `tc` mainnet, `tt` testnet, `td` devnet, `tr` regtest.
  The checksum covers the prefix, so an address from another network never validates.
  Because the version byte is zero, every address starts with `tc1`.
- **Burn address**: payload of 21 zero bytes (`tc1111111111111111111115gbLbA`). No public key
  hashes to it, so coins sent there can never move.

Wallet key handling (not consensus, but every TechnoCoin wallet does it this way):

- Passphrase: standard **BIP39**, 24 English words from 256 bits of cryptographically secure randomness.
- Seed: BIP39 PBKDF2-HMAC-SHA512 (2048 rounds, salt `"mnemonic" + optional extra passphrase`).
- Keys: **SLIP-0010** Ed25519 derivation, path `m/44'/84184'/account'/index'`.

## 4. Transactions

### 4.1 Transfer (type 1)

| Field | Encoding | Notes |
|---|---|---|
| version | u8 | `1` |
| network_id | u8 | 1 mainnet, 2 testnet, 3 regtest, 4 devnet |
| type | u8 | `1` |
| sender_public_key | 32 bytes | the sender is `payload(sender_public_key)` |
| nonce | u64 | must equal the sender's count of earlier transfers |
| fee | u64 | goes to the miner; may be 0 |
| output_count | u8 | 1–255 |
| outputs | output_count × (address 21 bytes, amount u64) | every amount ≥ 1 |
| memo | var8 | optional note, ≤ 255 bytes |
| signature | 64 bytes | |

- `signature = Ed25519_sign(key, "TechnoCoin/tx/1\x00" || all bytes before the signature)`.
  It covers every field, including each receiver's amount.
- `txid = H(all bytes, including the signature)`.
- A simple one-receiver transfer is 146 bytes.

### 4.2 Coinbase (type 0)

| Field | Encoding |
|---|---|
| version | u8 (`1`) |
| network_id | u8 |
| type | u8 (`0`) |
| height | u64 (must equal the block height, so every coinbase txid is unique) |
| address | 21 bytes (the miner) |
| amount | u64 |
| memo | var8 (miner message, ≤ 255 bytes) |

### 4.3 Stateless rules

A transaction is invalid if any of these fails:

- `network_id` matches the network.
- Every address payload has a known version.
- Coinbase: `0 ≤ amount ≤ MAX_AMOUNT`.
- Transfer: 1–255 outputs; each output amount in `[1, MAX_AMOUNT]`; fee in `[0, MAX_AMOUNT]`;
  `sum(outputs) + fee ≤ MAX_AMOUNT`; the signature is valid.

## 5. Blocks

### 5.1 Header (156 bytes)

| Field | Encoding |
|---|---|
| version | u32 (`1`) |
| height | u64 |
| prev_id | 32 bytes |
| merkle_root | 32 bytes |
| snapshot_root | 32 bytes (fingerprint of all balances at the latest snapshot, section 9) |
| timestamp | u64 (Unix seconds) |
| target | u256 |
| nonce | u64 (last field, so miners only rewrite the final 8 bytes) |

- `block_id = H(header)`. Blocks refer to each other by `block_id`.
- A block is `header || tx_count u32 || transactions`. The first transaction is the coinbase.

### 5.2 Merkle root

RFC 6962 tree over the txids in block order:
`leaf = H(0x00 || txid)`, `node = H(0x01 || left || right)`, and a list of n > 1 items splits at the
largest power of two smaller than n. There is no duplication of odd nodes, so two different
transaction lists can never share a root.

### 5.3 Stateless block rules

- `version == 1`; serialized size ≤ `max_block_size` (1,000,000 bytes).
- At least one transaction; the first is a coinbase and no other is.
- Coinbase height equals header height.
- No duplicate txids; the merkle root matches.
- Every transaction passes 4.3.

### 5.4 Header rules (need the parent)

- `height == parent.height + 1` and `prev_id == parent.block_id`.
- `timestamp > median of the timestamps of the parent and the 10 blocks before it`
  (fewer near genesis).
- `target ==` the ASERT target for this block (section 7).
- `snapshot_root ==` the root this block must carry (section 9).
- Proof of work holds (section 6).

Node rule, checked only when a block first arrives (not when replaying history):
`timestamp ≤ local clock + max_future_drift` (300 s; 10 s on devnet).

## 6. Proof of work

`pow = Argon2id(password = header bytes, salt = "TechnoCoin/PoW/1", memory, iterations, lanes = 1, output = 32 bytes)`
(RFC 9106, Argon2 version 1.3). Valid if `pow` read as a big-endian integer is `≤ target`.

| Network | Memory | Iterations |
|---|---|---|
| mainnet, testnet, devnet | 4 MiB | 1 |
| regtest | 8 KiB | 1 |

At 4 MiB a CPU core computes about 300 hashes per second, and checking one block costs about 3 ms.
Argon2id needs its memory for every hash, which removes the advantage of mining chips (ASICs)
and limits that of graphics cards.

`block_work(target) = 2^256 // (target + 1)`: the expected number of hashes to find the block.

## 7. Difficulty (ASERT)

Every block's target is computed from its parent and the genesis block (the anchor):

```
time_delta   = parent.timestamp − genesis.timestamp
height_delta = parent.height − genesis.height
exponent     = floor((time_delta − spacing × height_delta) × 65536 / half_life)
               # mainnet: spacing 60 s, half_life 3600 s; devnet: 1 s and 60 s
shifts       = exponent >> 16                    # floor
frac         = exponent − (shifts << 16)         # 0 ≤ frac < 65536
if shifts > 256:  target = pow_limit
elif shifts < −512: target = 1
else:
    factor = 65536 + ((195766423245049·frac + 971821376·frac² + 5127·frac³ + 2^47) >> 48)
    target = (genesis.target × factor) shifted left by `shifts` (right if negative), then >> 16
target = clamp(target, 1, pow_limit)
```

In plain words: the ideal schedule is one block per 60 seconds since genesis. Each hour ahead of
schedule doubles the difficulty and each hour behind halves it, applied smoothly on every block.
In simulation, a sudden 10× jump in mining power brings block times back within 10% of a minute
after about 3 hours (2× takes about 2 hours). Errors never accumulate: the chain only runs ahead of
schedule by about one hour per doubling of mining power, so long-run emission stays as promised.
The cubic approximates `2^x` with a
maximum error of about 0.013% using integers only, so every implementation agrees exactly.

Regtest skips this and always uses the genesis target.

## 8. State

The state maps every address to `(balance, nonce)`. It is never stored as a separate truth: it is
the result of applying every block of the chain in order, starting from empty.

Applying block `h` (after sections 4–7 pass):

1. **Maturity**: if `h ≥ 100`, credit the coinbase of block `h − 100` (its address, its amount).
   Mining rewards become spendable 100 blocks (about 100 minutes) after they are mined, so a
   reorganisation can't erase coins that were already spent onwards.
2. **Transfers**, in block order. For each:
   `sender.nonce == tx.nonce` and `sender.balance ≥ sum(outputs) + fee`; then debit the sender,
   increment its nonce, and credit every output. A later transfer in the same block sees the
   effects of earlier ones.
3. **Coinbase**: `coinbase.amount == block_reward + sum(fees)` exactly. It is credited in step 1 of
   block `h + 100`.

Any failure rejects the whole block and leaves the state unchanged. Nodes record each block's
previous account values, so a block can be undone exactly.

`block_reward = 10 TC` for every block, forever: 14,400 TC per day, 5,256,000 TC per year.
The largest single amount (`MAX_AMOUNT`, about 9.2 trillion TC) is not a supply cap; total supply
would take about 1.75 million years to reach it.

## 9. Balance snapshots

Once per chunk (1,440 blocks, about a day) the network fingerprints every balance, and later block
headers commit to that fingerprint. This lets new nodes start from recent balances instead of
replaying all history (section 12), lets nodes delete old blocks, and lets a wallet prove one
balance with a short proof.

- **Snapshot points**: the state right after each block whose `(height + 1)` is a multiple of 1,440
  (heights 1439, 2879, ...).
- **Snapshot root**: RFC 6962 merkle root over every non-empty account (balance or nonce not zero),
  sorted by address bytes, each leaf being `address 21 | balance u64 | nonce u64`. A state with no
  accounts has root `SHA-256("")`.
- **Which root a block carries**: with `delay = 10`,
  `finished = floor((height − delay) / 1440)`. If `finished < 1` the header carries
  `NO_SNAPSHOT` (32 zero bytes). Otherwise it carries the root of the state after block
  `finished × 1440 − 1`. So blocks 0–1449 carry `NO_SNAPSHOT`, blocks 1450–2889 carry the snapshot
  taken after block 1439, and so on. The 10-block delay gives nodes about ten minutes to compute the
  root in the background instead of stalling at the day boundary.

Computing the root costs one pass over all accounts per day: measured at about 5.5 seconds per
million accounts in the Python implementation (mostly the two million SHA-256 calls), done in the
background within the 10-block window.

## 10. Genesis

Block 0: `prev_id` all zeros, `snapshot_root = NO_SNAPSHOT`, target = `genesis_target`, one
coinbase with height 0 paying `block_reward` to the **burn address**, memo = the genesis message.
Nodes hard-code its timestamp, message, nonce and id. The mainnet message and timestamp are
chosen at launch.

## 11. Chain selection and finality (node rules)

- The best chain is the valid chain with the most total `block_work`; ties go to the one seen first.
- Switching chains undoes blocks back to the fork point and applies the other branch.
- **Finality**: a node never undoes a block that is 100 or more blocks below its tip. Deeper history
  is final. (A node isolated on a different chain for longer than that needs manual repair; the
  gain is that nobody can rewrite deep history, even with a burst of mining power.)

## 12. Block files, chunks, mega chunks and syncing

**Block files are the permanent record.** A node's database is only an index plus balances; it can
always be rebuilt from the files (`tc node --reindex`), and copying the `blocks` folder to another
machine copies the chain.

```
blocks/recent/<height>-<block id>.block   one block not yet sealed (any branch), its exact bytes
blocks/chunks/<index>.chunk               a sealed chunk: one day of final blocks
```

Files are written once and never changed. A recent file is deleted once its block is sealed into a
chunk (or, for a losing branch, once its height is final).

**Chunk** (one day): heights `k × chunk_size` to `k × chunk_size + chunk_size − 1` (1,440 blocks on
mainnet, 60 on devnet).
- Sealed once its last block is final (100 blocks deep); from then on it is final even for a node
  that has just rebuilt its database.
- `chunk_root` = RFC 6962 merkle root of the chunk's block ids.
- File layout (integers big-endian):
  `"TCCHUNK1" | network_id u8 | chunk index u32 | first height u64 | block count u32 | chunk_root 32
  | segment count u32 | segment table (offset u64, length u32 each) | segments | SHA-256 of all before`.
  Each segment is zlib-compressed and holds up to 64 blocks as `(length u32, block bytes)`. Grouping
  64 blocks keeps the file at about 40% of the raw size, while reading one block unpacks only its group.
- A chunk file is checked completely when read: checksum, structure, chunk root, and every block
  linking to the one before. Importing it then applies every consensus rule to every block, in one
  database transaction, with proof of work checked on all CPU cores: about 1.3 ms per block
  (a year of mainnet in about 12 minutes), against about 10 ms per block one at a time.
- Nodes serve sealed chunk files at `GET /v1/chunks/{index}`. `tc read FILE` prints any block or
  chunk file as JSON after checking it.

**Mega chunk** (about a year): chunks `m × 365` to `m × 365 + 364`.
- `mega_root` = RFC 6962 merkle root of its 365 chunk roots: one hash that fingerprints a year, and
  short proofs that a block or payment belongs to that year.
- Nodes keep the balance snapshot at the end of every mega chunk permanently. Archives may also
  offer a year as one bundle download, built from the same chunk files.

Chunk and mega-chunk roots are computed from block ids, so they are not consensus rules. More
levels (a decade, all of history) can be added at any time without changing the protocol.

**Syncing** today: headers after a block locator, then blocks in batches (section 16). Planned:
download sealed chunk files over HTTP for old history. Eventually either:
- **Full sync**: every chunk from genesis, applying every block.
- **Fast sync**: the balances at the latest final snapshot (checked against the `snapshot_root` in
  the headers), the `coinbase_maturity` blocks ending at the snapshot (their rewards mature after
  it), then every block after the snapshot.

**Pruning**: a node may delete blocks older than its latest mega-chunk snapshot, keeping headers
and snapshots. Archive nodes keep everything.

## 13. Networks

| | mainnet | testnet | devnet | regtest |
|---|---|---|---|---|
| purpose | the real network | public rehearsal | your own computer | automated tests |
| network_id | 1 | 2 | 4 | 3 |
| address prefix | `tc` | `tt` | `td` | `tr` |
| default port | 64184 | 64185 | 64187 | 64186 |
| block time | 60 s | 60 s | **1 s** | (no retarget) |
| ASERT half-life | 1 hour | 1 hour | 1 minute | none |
| chunk | 1,440 blocks (a day) | same | 60 blocks (a minute) | 1,440 |
| max future drift | 300 s | 300 s | 10 s | 300 s |
| proof of work | Argon2id 4 MiB | same | same | Argon2id 8 KiB |
| genesis target | ~18,000 hashes | same | ~300 hashes | ~2 hashes |
| pow_limit (easiest) | ~600 hashes | same | ~10 hashes | ~2 hashes |
| genesis | mined at launch | mined at launch | mined when a devnet is created | fixed |

Every network pays 10 TC per block, matures rewards after 100 blocks and finalizes 100 blocks deep.
Devnet is mainnet at 60x speed: the same rules and mining work, and the same behaviour counted in
blocks, so it shows how the real chain behaves in a few minutes. Each devnet mines its own genesis
block when it is first started, stamped with the current time (difficulty is scheduled from
genesis, so an old genesis would make a new devnet start far behind schedule).

The mainnet genesis target is sized so that one CPU core finds the first blocks in about a minute.
It will be revisited just before launch.

## 14. Node policy (not consensus)

Each node chooses these; changing them never splits the network. Defaults:

- A transfer enters the mempool only if it would be valid on top of the current tip plus the
  sender's other waiting transfers: consecutive nonces, and a **confirmed** balance that covers all
  of them. (Coins received but not yet in a block can't be spent yet.)
- Minimum fee: 1 base unit per byte (0.000146 TC for a simple payment). Set to 0 to relay free
  transfers. Consensus allows a fee of 0, so a miner can always include its own transfers for free.
- A waiting transfer can be replaced by one with the same sender and nonce paying at least 25% more
  per byte.
- Limits: 50 MB of waiting transfers, 64 per sender, 14 days before an unmined transfer is dropped.
  When full, the lowest fee per byte leaves first (only a sender's last transfer can leave, so no
  nonce gaps appear).
- After blocks are undone in a chain switch, their transfers return to the mempool if still valid.
- Miners take transfers by fee per byte, highest first, keeping each sender's nonce order, up to
  the block size limit.

## 15. Node API (version 1)

One server per node, one port (the network's default port), listening on this computer only unless
told otherwise. Interactive documentation is served at `/docs`.

- Amounts are strings in TC with all 6 decimals (`"12.500000"`), so no client ever rounds them.
  Ids are lowercase hex; addresses are in text form.
- Errors: `{"error": code, "detail": text}` with a 4xx status. Codes are the validation codes of
  this document (`insufficient-funds`, `nonce-gap`, `bad-signature`, ...) plus `bad-hex`,
  `bad-encoding`, `bad-address`, `unknown-block`, `unknown-transaction`, `too-large`.

| Endpoint | |
|---|---|
| `GET /v1/status` | network, genesis, height, tip, difficulty, reward, minimum fee, mempool size |
| `GET /v1/blocks/{height or id}` | a block with its transactions; `?format=hex` for raw bytes |
| `GET /v1/tx/{txid}` | a transaction, `pending` or `confirmed` (height, confirmations) |
| `POST /v1/tx` | `{"hex": ...}` submit a signed transfer |
| `GET /v1/mempool` | waiting transfers |
| `GET /v1/address/{address}` | `balance`, `available` (balance minus waiting spends), `pending_in`, `pending_out`, `immature` (unlocking rewards), `nonce`, `next_nonce` |
| `GET /v1/address/{address}/history` | transactions touching the address, newest first, with `kind` and signed `amount` |
| `GET /v1/mining/template?address=` | a block ready to mine (nonce 0), its target and Argon2id settings |
| `POST /v1/mining/submit` | `{"hex": ...}` a mined block |
| `WS /v1/ws` | live events |

WebSocket: send `{"subscribe": ["blocks", "mempool", "address:<address>"]}` (any number of times).
The node answers `{"event": "subscribed", ...}` and then pushes `block`, `reorg`, `tx` and `address`
events (`address` events say `pending` or `confirmed`). A client that falls more than 1,000 events
behind is disconnected.

## 16. Peer-to-peer

Nodes talk over WebSocket at `/v1/p2p` on the node's port, one JSON object per message, each with a
`type`. Blocks, headers and transactions travel as hex. Messages are at most 4 MB.

| Message | Meaning |
|---|---|
| `hello` | first message both ways: protocol version, network, genesis id, random node id, height, total work, listen URL |
| `inv` | "I have these": block and/or transaction ids (at most 1,000) |
| `get_data` | "send me these" |
| `block`, `tx` | the data itself |
| `not_found` | ids we were asked for but don't have |
| `get_headers` | a block locator: our ids, the last 10 then ever bigger steps back, ending with genesis |
| `headers` | up to 2,000 headers after the first locator block the peer also has on its active chain |
| `get_peers`, `peers` | addresses of other nodes (at most 100) |

- **Handshake**: a different network or genesis, an unknown protocol version, or no hello within
  10 seconds disconnects. A node that reaches itself (same node id) or a node it's already
  connected to closes the extra connection and doesn't dial that address again.
- **Spreading news**: every new tip and every accepted transfer (including transfers that come back
  after a chain switch) is announced by id to peers not known to have it; peers fetch what they
  lack. A newly connected peer is told about everything in the mempool.
- **Catching up**: when a peer has more total work (from its hello), or sends a block whose parent
  is missing, the node asks it for headers after its locator and downloads those blocks 64 at a
  time, in order. One peer at a time; a peer that stalls for 30 seconds is dropped for another.
- **Misbehaviour**: malformed or oversized messages, blocks that break a rule, or claiming blocks it
  can't send get a peer disconnected. A block from slightly in the future or from a fork below our
  finality is not treated as misbehaviour, but that peer isn't used for catching up.
- Each peer has its own send queue (5,000 messages); a peer that can't keep up is disconnected.
- Nodes keep up to 8 outbound connections (`--peer` addresses first, then learned ones) and accept
  up to 32 inbound.

Planned: chunk files for fast bulk sync (section 12), remembering learned addresses across restarts,
per-peer rate limits.
