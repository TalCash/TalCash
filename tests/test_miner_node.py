"""The multi-process miner, devnet creation, and a local node mining for real."""

import asyncio
from dataclasses import replace

import pytest

from technocoin.cli import main
from technocoin.core.genesis import genesis_block
from technocoin.core.params import DEVNET, MAINNET, REGTEST, target_for
from technocoin.core.pow import meets_target
from technocoin.crypto.address import encode_address
from technocoin.miner.engine import Miner
from technocoin.node.chain import ChainManager
from technocoin.node.network import GENESIS_FILE, load_params, reset_devnet
from technocoin.node.server import mining_loop
from technocoin.node.service import NodeService
from technocoin.node.store import Store
from technocoin.wallet.keystore import INSECURE_FAST
from technocoin.wallet.wallet import Wallet

from chainutil import named_key

MINER = named_key("miner")


def test_miner_finds_nonces_and_switches_jobs():
    header = replace(genesis_block(REGTEST).header, target=target_for(300), nonce=0)
    with Miner(workers=2) as miner:
        miner.mine(header, REGTEST.pow)
        nonce = miner.wait(timeout=60)
        assert nonce is not None and meets_target(replace(header, nonce=nonce), REGTEST.pow)

        second = replace(header, timestamp=header.timestamp + 1)
        miner.mine(second, REGTEST.pow)
        nonce = miner.wait(timeout=60)
        assert nonce is not None and meets_target(replace(second, nonce=nonce), REGTEST.pow)

        miner.mine(replace(header, target=1), REGTEST.pow)  # practically impossible
        assert miner.wait(timeout=0.5) is None
        assert miner.total_hashes() > 0


def test_devnet_gets_its_own_genesis(tmp_path):
    with pytest.raises(RuntimeError, match="no devnet"):
        load_params("devnet", tmp_path, create=False)
    params = load_params("devnet", tmp_path)
    assert (tmp_path / "devnet" / GENESIS_FILE).exists()
    assert params.target_spacing == 1 and params.address_prefix == "td"
    assert load_params("devnet", tmp_path).genesis_id == params.genesis_id  # restarts reuse it

    node = ChainManager(Store(":memory:"), params)
    assert node.genesis.block_id == params.genesis_id
    assert meets_target(node.genesis.header, DEVNET.pow)

    assert [p.name for p in reset_devnet(tmp_path)] == [GENESIS_FILE]
    assert load_params("mainnet", tmp_path) is MAINNET


def test_mining_loop_mines_blocks():
    lines = []
    service = NodeService(REGTEST, Store(":memory:"), log=lines.append)
    asyncio.run(mining_loop(service, MINER.address, workers=2, blocks=3))
    assert service.chain.tip_height == 3
    assert sum("[mined" in line for line in lines) == 3
    service.close()


def test_cli_node_command(tmp_path, capsys):
    """Runs the real node: API server plus miner, stopping after two blocks."""
    address = encode_address(MINER.address, REGTEST.address_prefix)
    args = ["--network", "regtest", "--datadir", str(tmp_path), "node", "--mine", address,
            "--blocks", "2", "--threads", "2", "--port", "0"]
    assert main(args) == 0
    assert "#2 " in capsys.readouterr().out
    assert main(args) == 0  # restarting continues the same chain
    assert "#4 " in capsys.readouterr().out
    assert main(["--network", "regtest", "--datadir", str(tmp_path), "node", "--mine", "tc1bad"]) == 1


def test_cli_mines_to_the_wallet_by_default(tmp_path, capsys):
    wallet, _ = Wallet.create(tmp_path / "regtest" / "wallet.json", REGTEST, "password1", strength=INSECURE_FAST)
    args = ["--network", "regtest", "--datadir", str(tmp_path), "node", "--mine", "--blocks", "1", "--threads", "1",
            "--port", "0"]
    assert main(args) == 0
    assert f"paying {wallet.addresses[0].address}" in capsys.readouterr().out
