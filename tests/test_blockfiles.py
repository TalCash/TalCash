"""Block files: the permanent record the database can always be rebuilt from."""

import copy
import json
import shutil
import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from technocoin.cli import main
from technocoin.core.block import Block, block_from_bytes
from technocoin.core.params import REGTEST
from technocoin.core.errors import ValidationError
from technocoin.core.pow import meets_target, mine
from technocoin.crypto.address import encode_address
from technocoin.node.api import create_app
from technocoin.node.blockfiles import ChunkError, decode_chunk, encode_chunk
from technocoin.node.chain import ChainManager, Outcome
from technocoin.node.reindex import reindex
from technocoin.node.service import NodeService
from technocoin.node.store import Store

from chainutil import TestChain, make_transfer, named_key

# Chunks of 5 blocks, final 10 deep: a 40-block chain seals chunks 0-5 (heights 0-29).
PARAMS = replace(REGTEST, coinbase_maturity=3, chunk_size=5, snapshot_delay=2, finality_depth=10)
NOW = 2_000_000_000
ALICE, BOB, MINER_B = named_key("alice"), named_key("bob"), named_key("miner-b")


def build_history():
    """A 40-block chain with a payment, plus a losing branch early on and one near the tip."""
    main_chain = TestChain(PARAMS)
    main_chain.mine_blocks(4, miner=ALICE.address)
    main_chain.add(main_chain.build_block([make_transfer(PARAMS, ALICE, 0, [(BOB.address, 3_000_000)], fee=200)]))
    main_chain.mine_blocks(6)  # height 11
    early = copy.deepcopy(main_chain)
    early.mine_blocks(2, miner=MINER_B.address)  # heights 12-13, loses
    main_chain.mine_blocks(27)  # height 38
    late = copy.deepcopy(main_chain)
    late.mine_blocks(1, miner=MINER_B.address)  # height 39, loses
    main_chain.mine_blocks(2)  # height 40
    return main_chain, early.blocks[12:], late.blocks[39:]


def feed(node, main_chain, early, late):
    for block in main_chain.blocks[1:14]:
        node.submit_block(block)
    for block in early:
        assert node.submit_block(block).outcome is Outcome.SIDE_BRANCH
    for block in main_chain.blocks[14:40]:
        node.submit_block(block)
    for block in late:
        assert node.submit_block(block).outcome is Outcome.SIDE_BRANCH
    node.submit_block(main_chain.blocks[40])


@pytest.fixture
def history():
    return build_history()


@pytest.fixture
def node(tmp_path, history):
    chain = ChainManager(Store(tmp_path / "chain.sqlite"), PARAMS, clock=lambda: NOW)
    feed(chain, *history)
    yield chain
    chain.store.close()


def test_chunk_file_round_trip_and_every_damaged_byte_is_caught(history):
    main_chain = history[0]
    raw = [b.serialize() for b in main_chain.blocks[:5]]
    data, _ = encode_chunk(PARAMS.network_id, 0, 0, raw)
    chunk = decode_chunk(data)
    assert chunk.blocks == raw and chunk.index == 0 and chunk.first_height == 0
    for position in range(len(data)):
        damaged = bytearray(data)
        damaged[position] ^= 0x01
        with pytest.raises(ChunkError):
            decode_chunk(bytes(damaged))
    with pytest.raises(ChunkError):
        decode_chunk(data[:-1])
    with pytest.raises(ChunkError, match="not a TechnoCoin chunk"):
        decode_chunk(b"hello")
    reordered, _ = encode_chunk(PARAMS.network_id, 0, 0, [raw[0], raw[2], raw[1], raw[3], raw[4]])
    with pytest.raises(ChunkError, match="chain"):
        decode_chunk(reordered)


def test_blocks_live_in_files_and_final_days_are_sealed(node, history, tmp_path):
    main_chain, early, late = history
    files = node.store.files
    assert node.store.sealed_chunks() == 6
    assert files.chunk_indexes() == [0, 1, 2, 3, 4, 5]
    recent = files.recent_blocks()
    assert [h for h, _ in recent] == [*range(30, 39), 39, 39, 40]  # unsealed blocks, including the late loser
    assert late[0].block_id in {i for _, i in recent}
    # The early losing branch is below the sealed part: gone from the files and the index.
    for block in early:
        assert node.store.header(block.block_id) is None
    # Every block of the active chain reads back byte for byte, from a chunk or a recent file.
    for block in main_chain.blocks:
        assert node.store.block_bytes(block.block_id) == block.serialize()
    # The database no longer holds any block bytes.
    tables = {row[0] for row in sqlite3.connect(tmp_path / "chain.sqlite").execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "block_data" not in tables
    assert node.finalized_height() == 30


def test_reindex_rebuilds_everything_from_the_files_alone(node, history, tmp_path):
    main_chain, _, late = history
    before = dict(node.store.all_accounts())
    node.store.close()

    # Copy only the blocks folder somewhere else, as if moving to another machine.
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(tmp_path / "blocks", elsewhere / "blocks")
    assert reindex(PARAMS, elsewhere, log=lambda line: None) == 40

    rebuilt = ChainManager(Store(elsewhere / "chain.sqlite"), PARAMS, clock=lambda: NOW)
    assert rebuilt.tip.block_id == main_chain.tip.block_id
    assert dict(rebuilt.store.all_accounts()) == before == main_chain.state.accounts
    for height in main_chain.snapshots:
        assert rebuilt.store.snapshot_root(height) == main_chain.snapshot_root_at(height)
    assert rebuilt.store.header(late[0].block_id) is not None  # recent losing branch kept
    payment = main_chain.blocks[5].transactions[1]
    assert rebuilt.store.find_tx(payment.txid).height == 5
    rebuilt.store.close()


def test_a_crash_while_sealing_is_repaired_on_restart(node, history, tmp_path):
    main_chain = history[0]
    files = node.store.files
    # As if the node died after updating the index but before deleting the recent files...
    for block in main_chain.blocks[25:28]:
        files.write_recent(block)
    # ...and as if it died after writing the next chunk file but before updating the index.
    files.write_chunk(PARAMS.network_id, 6, 30, [b.serialize() for b in main_chain.blocks[30:35]])
    node.store.close()

    restarted = ChainManager(Store(tmp_path / "chain.sqlite"), PARAMS, clock=lambda: NOW)
    assert [h for h, _ in restarted.store.files.recent_blocks()][:3] == [30, 31, 32]  # leftovers tidied
    assert restarted.store.sealed_chunks() == 6  # chunk 6 isn't final yet; its early file changes nothing
    for block in main_chain.blocks:
        assert restarted.store.block_bytes(block.block_id) == block.serialize()
    restarted.store.close()


def test_the_reader(node, tmp_path, capsys):
    chunk_path = tmp_path / "blocks" / "chunks" / "000002.chunk"
    assert main(["read", str(chunk_path)]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["type"] == "chunk" and info["heights"] == [10, 14] and "OK" in info["verified"]

    assert main(["read", str(chunk_path), "--blocks"]) == 0
    blocks = json.loads(capsys.readouterr().out)["block_list"]
    assert [b["height"] for b in blocks] == [10, 11, 12, 13, 14]

    height, block_id = node.store.files.recent_blocks()[-1]
    recent_path = tmp_path / "blocks" / node.store.files.recent_name(height, block_id)
    assert main(["read", str(recent_path)]) == 0
    block = json.loads(capsys.readouterr().out)
    assert block["type"] == "block" and block["height"] == 40 and block["id"] == block_id.hex()

    damaged = tmp_path / "damaged.chunk"
    data = bytearray(chunk_path.read_bytes())
    data[100] ^= 1
    damaged.write_bytes(bytes(data))
    assert main(["read", str(damaged)]) == 1
    assert "checksum" in capsys.readouterr().err


def test_import_rejects_a_chunk_with_a_bad_block(history):
    main_chain = history[0]
    node = ChainManager(Store(":memory:"), PARAMS, clock=lambda: NOW)
    good = [b.serialize() for b in main_chain.blocks[:5]]
    bad_block = main_chain.blocks[3]
    tampered = Block(replace(bad_block.header, timestamp=bad_block.header.timestamp - 1000), bad_block.transactions)
    data, _ = encode_chunk(PARAMS.network_id, 0, 0, [*good[:3], tampered.serialize(), good[4]])
    with pytest.raises(ChunkError):  # the header change breaks the links to block 4
        node.import_chunk(data)

    # A well-formed chunk file whose last block has invalid proof of work (links intact).
    last = main_chain.blocks[4]
    nonce = next(n for n in range(1000) if not meets_target(replace(last.header, nonce=n), PARAMS.pow))
    no_work = Block(replace(last.header, nonce=nonce), last.transactions)
    data, _ = encode_chunk(PARAMS.network_id, 0, 0, [*good[:4], no_work.serialize()])
    with pytest.raises(ValidationError, match="bad-pow"):
        node.import_chunk(data)
    assert node.tip_height == 0 and node.store.sealed_chunks() == 0
    assert not node.store.files.has_chunk(0)  # a rejected chunk isn't kept

    ok, _ = encode_chunk(PARAMS.network_id, 0, 0, good)
    assert node.import_chunk(ok) == 4 and node.tip_height == 4


def test_the_api_serves_chunk_files(tmp_path):
    app = create_app(lambda: NodeService(PARAMS, Store(tmp_path / "chain.sqlite"), log=lambda line: None))
    alice = encode_address(ALICE.address, PARAMS.address_prefix)
    with TestClient(app) as api:
        for _ in range(20):
            template = block_from_bytes(bytes.fromhex(api.get("/v1/mining/template", params={"address": alice})
                                                      .json()["hex"]))
            mined = Block(mine(template.header, PARAMS.pow), template.transactions)
            api.post("/v1/mining/submit", json={"hex": mined.serialize().hex()})
        assert api.get("/v1/status").json()["sealed_chunks"] == 2  # heights 0-9 are final at tip 20
        response = api.get("/v1/chunks/1")
        assert response.content == (tmp_path / "blocks" / "chunks" / "000001.chunk").read_bytes()
        assert decode_chunk(response.content).first_height == 5
        assert api.get("/v1/chunks/2").status_code == 404
