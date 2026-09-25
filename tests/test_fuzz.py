"""Fuzzing: random and damaged input for everything that reads data from outside.

The rule everywhere: bad input is refused with the one error meant for it (DecodeError,
ChunkError, ValidationError, ValueError, ProtocolError, a 4xx answer), never anything else, and
anything accepted comes out byte for byte the same when written back (each block and transfer has
exactly one encoding, so nobody can change a txid without changing the transfer).

Deterministic (fixed seeds). For a long run: TC_FUZZ_ROUNDS=100 python -m pytest tests/test_fuzz.py
"""

import asyncio
import json
import os
import random
import string
from dataclasses import replace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from talcash.core.amounts import parse_amount
from talcash.core.block import block_from_bytes, check_block, header_from_bytes
from talcash.core.errors import DecodeError, ValidationError
from talcash.core.params import REGTEST
from talcash.core.tx import check_transaction, transaction_from_bytes
from talcash.crypto.address import decode_address, encode_address
from talcash.crypto.mnemonic import mnemonic_to_entropy
from talcash.node.api import ApiPolicy, create_app
from talcash.node.blockfiles import ChunkError, decode_chunk, encode_chunk
from talcash.node.chain import check_chunk_file
from talcash.node.p2p import P2PConfig, Peer, PeerManager, ProtocolError
from talcash.node.service import NodeService
from talcash.node.store import Store

from chainutil import TestChain, make_transfer, named_key

ROUNDS = int(os.environ.get("TC_FUZZ_ROUNDS", "1"))
PARAMS = replace(REGTEST, coinbase_maturity=3, chunk_size=5, snapshot_delay=2, finality_depth=10)
ALICE, BOB, CAROL = named_key("alice"), named_key("bob"), named_key("carol")
PREFIX = PARAMS.address_prefix


def sample_chain() -> TestChain:
    """A short chain with payments of every shape in it."""
    chain = TestChain(PARAMS)
    chain.mine_blocks(4, miner=ALICE.address)
    chain.add(chain.build_block([
        make_transfer(PARAMS, ALICE, 0, [(BOB.address, 5)], fee=300),
        make_transfer(PARAMS, ALICE, 1, [(k.address, i + 1) for i, k in enumerate([BOB, CAROL] * 20)],
                      fee=3000, memo=b"many receivers"),
    ]))
    chain.mine_blocks(6)
    return chain


CHAIN = sample_chain()
TX_SEEDS = [tx.serialize() for block in CHAIN.blocks for tx in block.transactions]
BLOCK_SEEDS = [block.serialize() for block in CHAIN.blocks]
HEADER_SEEDS = [block.header.serialize() for block in CHAIN.blocks]


def mutate(rng: random.Random, data: bytes) -> bytes:
    """One to three random edits: flipped bits, changed/inserted/deleted/duplicated bytes, extreme numbers."""
    data = bytearray(data)
    for _ in range(rng.randint(1, 3)):
        kind = rng.randrange(8)
        spot = rng.randrange(len(data) + 1)
        if kind == 0 and data:
            data[min(spot, len(data) - 1)] ^= 1 << rng.randrange(8)
        elif kind == 1 and data:
            data[min(spot, len(data) - 1)] = rng.choice([0, 0xFF, 0x7F, 0x80, rng.randrange(256)])
        elif kind == 2:
            data[spot:spot] = rng.randbytes(rng.randint(1, 40))
        elif kind == 3:
            del data[spot:spot + rng.randint(1, 40)]
        elif kind == 4:
            del data[spot:]
        elif kind == 5:
            data[spot:spot] = data[max(0, spot - 50):spot]
        elif kind == 6:  # a length or count field set to something extreme
            size = rng.choice([1, 4, 8])
            data[spot:spot + size] = rng.choice([b"\xff", b"\x00", b"\x80"]) * size
        else:
            data += rng.randbytes(rng.randint(1, 10))
    return bytes(data)


def fuzz_inputs(rng: random.Random, seeds: list[bytes], count: int):
    for _ in range(count):
        if rng.random() < 0.1:
            yield rng.randbytes(rng.randrange(400))
        else:
            yield mutate(rng, rng.choice(seeds))


# --- binary decoders ----------------------------------------------------------------------


@pytest.mark.parametrize("decode, check, seeds", [
    (transaction_from_bytes, lambda tx: check_transaction(tx, PARAMS), TX_SEEDS),
    (block_from_bytes, lambda block: check_block(block, PARAMS), BLOCK_SEEDS),
    (header_from_bytes, None, HEADER_SEEDS),
], ids=["transaction", "block", "header"])
def test_decoders_refuse_garbage_cleanly_and_accept_only_canonical_encodings(decode, check, seeds):
    rng = random.Random(1)
    accepted = 0
    for data in fuzz_inputs(rng, seeds, 3000 * ROUNDS):
        try:
            thing = decode(data)
        except DecodeError:
            continue
        accepted += 1
        assert thing.serialize() == data  # one encoding only
        if check is not None:
            try:
                check(thing)
            except ValidationError:
                pass
    assert accepted > 0  # the mutations do reach the checks behind the decoder


def test_chunk_files_detect_every_change():
    blocks = [block.serialize() for block in CHAIN.blocks[5:10]]
    original, _ = encode_chunk(PARAMS.network_id, 1, 5, blocks)
    rng = random.Random(2)
    for data in fuzz_inputs(rng, [original], 1500 * ROUNDS):
        try:
            decoded = decode_chunk(data)
        except ChunkError:
            continue
        assert data == original and decoded.blocks == blocks  # only an unchanged file gets through
        check_chunk_file(data, PARAMS)


def test_a_chunk_that_unpacks_to_gigabytes_is_refused_without_unpacking_it():
    import zlib
    from talcash.node import blockfiles

    bomb = zlib.compress(bytes(blockfiles.MAX_SEGMENT_BYTES + 1), 9)  # a few hundred kB packed
    assert len(bomb) < 1_000_000
    with pytest.raises(zlib.error, match="more than 64 full blocks"):
        blockfiles._unpack_segment(bomb)


def test_a_segment_must_be_exactly_one_complete_zlib_stream():
    import zlib
    from talcash.node import blockfiles

    packed = blockfiles._pack_segment([block.serialize() for block in CHAIN.blocks[:3]])
    assert len(blockfiles._unpack_segment(packed)) == 3
    for crafted in (packed[:-4], packed + b"extra"):  # no end-of-stream check value / bytes after the end
        with pytest.raises(zlib.error, match="cut short or has extra bytes"):
            blockfiles._unpack_segment(crafted)


# --- text people type or paste ----------------------------------------------------------------


def random_text(rng: random.Random) -> str:
    alphabet = string.printable + "١٢٣äöü€𝟘​\x00"
    return "".join(rng.choice(alphabet) for _ in range(rng.randrange(60)))


def test_addresses_amounts_and_mnemonics_refuse_garbage_with_value_error():
    rng = random.Random(3)
    good_address = encode_address(ALICE.address, PREFIX)
    for _ in range(3000 * ROUNDS):
        text = random_text(rng) if rng.random() < 0.5 else mutate(rng, good_address.encode()).decode("latin-1")
        try:
            payload = decode_address(text, PREFIX)
            assert encode_address(payload, PREFIX) == text
        except ValueError:
            pass
        try:
            assert parse_amount(text) >= 0
        except ValueError:
            pass
        try:
            mnemonic_to_entropy(text)
        except ValueError:
            pass
    with pytest.raises(ValueError, match="too long"):
        decode_address(PREFIX + "1" * 4_000_000, PREFIX)  # refused at once, not after minutes of decoding
    with pytest.raises(ValueError):
        parse_amount("١٢")  # digits from other alphabets aren't amounts


# --- peer-to-peer messages ------------------------------------------------------------------

JSON_VALUES = [None, True, False, 0, -1, 1, 2**63, 2**64 + 1, -(2**70), 1.5, 1e308, float("inf"), float("nan"),
               "", "zz", "0", "00" * 32, "ff" * 33, "ab" * 200, "x" * 5000, [], [[]], {}, {"a": 1},
               ["00" * 32] * 3, [1, 2], ["zz"], ["00" * 32] * 1001, "ws://127.0.0.1:1/v1/p2p"]


def peer_message_templates(service: NodeService) -> list[dict]:
    ids = [block.block_id.hex() for block in CHAIN.blocks[:4]]
    return [
        {"type": "inv", "blocks": ids[:2], "txs": [os.urandom(32).hex()]},
        {"type": "get_data", "blocks": ids, "txs": [TX_SEEDS[-1].hex()[:64]]},
        {"type": "block", "hex": BLOCK_SEEDS[3].hex()},
        {"type": "block", "hex": BLOCK_SEEDS[5].hex()},
        {"type": "tx", "hex": CHAIN.blocks[5].transactions[1].serialize().hex()},
        {"type": "not_found", "blocks": ids[:1], "txs": []},
        {"type": "get_headers", "locator": ids},
        {"type": "headers", "headers": [h.hex() for h in HEADER_SEEDS[1:4]]},
        {"type": "get_peers"},
        {"type": "peers", "urls": ["ws://127.0.0.1:1/v1/p2p", "http://nope"]},
        {"type": "something-new", "x": 1},
    ]


def mutate_message(rng: random.Random, message: dict) -> dict:
    message = dict(message)
    for _ in range(rng.randint(1, 3)):
        kind = rng.randrange(5)
        keys = [k for k in message if k != "type"]
        if kind == 0 and keys:
            message[rng.choice(keys)] = rng.choice(JSON_VALUES)
        elif kind == 1 and keys:
            del message[rng.choice(keys)]
        elif kind == 2:
            message[rng.choice(["hex", "blocks", "txs", "locator", "headers", "urls", "extra"])] = rng.choice(JSON_VALUES)
        elif kind == 3 and isinstance(message.get("hex"), str) and all(c in string.hexdigits for c in message["hex"]):
            message["hex"] = mutate(rng, bytes.fromhex(message["hex"][:len(message["hex"]) // 2 * 2])).hex()
        elif kind == 4:
            message["type"] = rng.choice(["inv", "get_data", "block", "tx", "headers", "hello", "", "INV"])
    return message


def test_peer_messages_are_handled_or_refused_with_a_protocol_error():
    logs: list[str] = []

    async def fuzz() -> int:
        service = NodeService(PARAMS, Store(":memory:"), log=logs.append)
        for block in CHAIN.blocks[1:4]:
            service.submit_block(block, source="setup")
        peers = PeerManager(service, P2PConfig())
        templates = peer_message_templates(service)
        rng = random.Random(4)
        refused = 0

        async def quiet(*args):
            return None

        peer = None
        for round_ in range(2000 * ROUNDS):
            if round_ % 50 == 0:
                peer = Peer(quiet, quiet, quiet, outbound=False, label="fuzz", host="203.0.113.1")
                peer.ready, peer.node_id = True, f"{round_:032x}"
                peers.peers.add(peer)
                peers.sync_peer = peer if rng.random() < 0.5 else None
                peer.awaiting_headers = True
            text = json.dumps(mutate_message(rng, rng.choice(templates)))
            if rng.random() < 0.1:
                text = mutate(rng, text.encode()).decode("latin-1")
            try:
                peers._handle(peer, peers._parse(text))
            except ProtocolError:
                refused += 1
            if round_ % 25 == 0:
                await asyncio.sleep(0.01)  # let background checks (headers, chunks) run too
        for task in list(peers._tasks):
            task.cancel()
        return refused

    refused = asyncio.run(fuzz())
    assert refused > 100  # plenty of it was refused...
    assert not [line for line in logs if "Traceback" in line]  # ...and nothing broke inside


def test_hello_messages_are_accepted_or_refused_with_a_protocol_error():
    service = NodeService(PARAMS, Store(":memory:"), log=lambda line: None)
    peers = PeerManager(service, P2PConfig())
    hello = peers._hello()
    rng = random.Random(5)

    async def run() -> None:
        for _ in range(1000 * ROUNDS):
            message = dict(hello, node_id=os.urandom(16).hex())
            for _ in range(rng.randint(1, 3)):
                message[rng.choice(list(hello) + ["extra"])] = rng.choice(JSON_VALUES)
            peer = Peer(None, None, None, outbound=False, label="fuzz", host="203.0.113.2")
            try:
                peers._handle_hello(peer, peers._parse(json.dumps(message)))
            except ProtocolError:
                pass
            peers.peers.clear()
            peers.sync_peer = None

    asyncio.run(run())


def test_unparseable_text_is_a_protocol_error():
    peers = PeerManager(NodeService(PARAMS, Store(":memory:"), log=lambda line: None), P2PConfig())
    for text in ["", "[" * 100_000, "{\"type\": 1}", "null", "\"hello\"", "{\"type\": \"inv\"", "NaN", "\x00"]:
        with pytest.raises(ProtocolError):
            peers._parse(text)


# --- the HTTP and WebSocket API ----------------------------------------------------------------


def test_the_api_never_fails_with_a_server_error():
    """TestClient raises on any unhandled exception (a 500), so this checks every answer is deliberate."""
    rng = random.Random(6)
    service_holder = {}

    def open_service():
        service = NodeService(PARAMS, Store(":memory:"), log=lambda line: None)
        for block in CHAIN.blocks[1:6]:
            service.submit_block(block, source="setup")
        service_holder["service"] = service
        return service

    good = {"address": encode_address(ALICE.address, PREFIX), "block": CHAIN.blocks[2].block_id.hex(),
            "tx": CHAIN.blocks[5].transactions[1].txid.hex()}

    def piece() -> str:
        return rng.choice([
            rng.choice(list(good.values())), str(rng.randrange(-5, 10**6)), str(10**40), "-1", "", "%00", "..",
            "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(rng.randrange(1, 80))),
            mutate(rng, rng.choice(list(good.values())).encode()).decode("latin-1").replace("/", "").replace("?", ""),
        ])

    paths = ["/v1/status", "/v1/blocks/{}", "/v1/tx/{}", "/v1/mempool", "/v1/address/{}",
             "/v1/address/{}/history", "/v1/mining/template", "/v1/chunks/{}", "/v1/genesis", "/v1/peers"]
    policy = ApiPolicy(public=True, requests_per_second=10**6, burst=10**6)
    with TestClient(create_app(open_service, policy=policy)) as stranger, \
            TestClient(create_app(open_service)) as local:
        for _ in range(600 * ROUNDS):
            api = rng.choice([stranger, local])
            path = rng.choice(paths).replace("{}", quote(piece(), safe=""))  # the node sees it decoded
            params = {rng.choice(["limit", "format", "address", "x"]): piece() for _ in range(rng.randrange(3))}
            response = api.get(path, params=params)
            assert response.status_code < 500, (path, params, response.text)

            body = rng.choice([{"hex": rng.choice(TX_SEEDS + BLOCK_SEEDS).hex()},
                               {"hex": mutate(rng, rng.choice(TX_SEEDS + BLOCK_SEEDS)).hex()},
                               {"hex": piece()}, {"hex": rng.choice(JSON_VALUES[:11])}, {}, [], "text"])
            target = rng.choice(["/v1/tx", "/v1/mining/submit"])
            response = api.post(target, json=body)
            assert response.status_code < 500, (target, body, response.text)

        for api in (stranger, local):
            with api.websocket_connect("/v1/ws") as socket:
                for _ in range(100 * ROUNDS):
                    choice = rng.randrange(4)
                    if choice == 0:
                        socket.send_text(mutate(rng, b'{"subscribe": ["blocks"]}').decode("latin-1"))
                    elif choice == 1:
                        socket.send_json({"subscribe": [rng.choice(JSON_VALUES[:22]) for _ in range(rng.randrange(5))]})
                    elif choice == 2:
                        socket.send_json({"subscribe": ["address:" + piece() for _ in range(3)]})
                    else:
                        socket.send_bytes(rng.randbytes(20))
                    assert socket.receive_json()["event"] in ("subscribed", "error")
