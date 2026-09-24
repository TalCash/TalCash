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
| Mempool policy | Planned (step 4) |
| Chunk files, mega chunks, fast sync, P2P, API | Planned (steps 5-6), outline only |

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
  `checksum = H(H(prefix || payload))[0:4]`. Prefixes: `tc` mainnet, `tt` testnet, `tr` regtest.
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
| network_id | u8 | 1 mainnet, 2 testnet, 3 regtest |
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
`timestamp ≤ local clock + 300 seconds`.

## 6. Proof of work

`pow = Argon2id(password = header bytes, salt = "TechnoCoin/PoW/1", memory, iterations, lanes = 1, output = 32 bytes)`
(RFC 9106, Argon2 version 1.3). Valid if `pow` read as a big-endian integer is `≤ target`.

| Network | Memory | Iterations |
|---|---|---|
| mainnet, testnet | 4 MiB | 1 |
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
exponent     = floor((time_delta − 60 × height_delta) × 65536 / half_life)      # half_life = 3600
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

## 12. Chunks, mega chunks and syncing (planned)

**Chunk** (one day): heights `k × 1440` to `k × 1440 + 1439`.
- Sealed once its last block is final. Stored as one compressed file holding its blocks and
  full transaction list.
- `chunk_root` = RFC 6962 merkle root of the chunk's block ids.
- Chunk files are the unit of syncing: small enough to fetch from many peers in parallel, check one
  by one and resume after a dropped connection.

**Mega chunk** (about a year): chunks `m × 365` to `m × 365 + 364`.
- `mega_root` = RFC 6962 merkle root of its 365 chunk roots: one hash that fingerprints a year, and
  short proofs that a block or payment belongs to that year.
- Nodes keep the balance snapshot at the end of every mega chunk permanently. Archives may also
  offer a year as one bundle download, built from the same chunk files.

Chunk and mega-chunk roots are computed from block ids, so they are not consensus rules. More
levels (a decade, all of history) can be added at any time without changing the protocol.

**Syncing** starts with headers: 156 bytes per block, checked for links, targets and proof of work,
so the node knows the chain with the most work before downloading any blocks. Then either:
- **Full sync**: every chunk from genesis, applying every block.
- **Fast sync**: the balances at the latest final snapshot (checked against the `snapshot_root` in
  the headers), the `coinbase_maturity` blocks ending at the snapshot (their rewards mature after
  it), then every block after the snapshot.

**Pruning**: a node may delete blocks older than its latest mega-chunk snapshot, keeping headers
and snapshots. Archive nodes keep everything.

## 13. Networks

| | mainnet | testnet | regtest |
|---|---|---|---|
| network_id | 1 | 2 | 3 |
| address prefix | `tc` | `tt` | `tr` |
| default port | 64184 | 64185 | 64186 |
| block time | 60 s | 60 s | 60 s (no retarget) |
| reward | 10 TC | 10 TC | 10 TC |
| genesis target | ~18,000 hashes per block | same | ~2 hashes |
| pow_limit (easiest) | ~600 hashes per block | same | ~2 hashes |

The genesis target is sized so that one CPU core finds the first blocks in about a minute. It will
be revisited just before launch.

## 14. Node policy (planned, not consensus)

- Mempool: a transfer is accepted if it would be valid on top of the current tip plus the sender's
  other pending transfers (consecutive nonces, enough balance for all of them).
- Minimum relay fee per byte (spam protection; each node can change it without a fork). Consensus
  allows a fee of 0, so miners may include their own transfers for free.
- Miners fill blocks by fee per byte, highest first.

## 15. Networking and API (planned)

- One server per node, one port: HTTP JSON API for requests, WebSocket for live events and for
  node-to-node gossip.
- Node handshake exchanges network id, genesis id, height and total work. Every message has a
  request id, a size limit and a checked schema; misbehaving peers are disconnected.
