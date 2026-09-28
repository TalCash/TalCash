"""A failed devnet join must not persist an invalid genesis or damage an existing one."""

import json

import httpx
import pytest

from talcash.core.genesis import build_genesis
from talcash.core.params import DEVNET
from talcash.node import network


def candidate():
    block = build_genesis(DEVNET, timestamp=12345, message=b"join test")
    return {"network": "devnet", "timestamp": block.header.timestamp,
            "message": block.coinbase.memo.hex(), "nonce": block.header.nonce, "id": block.block_id.hex()}


@pytest.mark.parametrize("broken", [
    {"id": "00" * 32}, {"message": "zz"}, {"nonce": -1}, {"timestamp": 2**64},
])
@pytest.mark.parametrize("existing", [False, True])
def test_invalid_join_leaves_configuration_unchanged(tmp_path, monkeypatch, broken, existing):
    path = tmp_path / "devnet" / network.GENESIS_FILE
    original = json.dumps(candidate()).encode()
    if existing:
        path.parent.mkdir()
        path.write_bytes(original)
    reply = {**candidate(), **broken}
    response = httpx.Response(200, json=reply, request=httpx.Request("GET", "http://peer/v1/genesis"))
    monkeypatch.setattr(network.httpx, "get", lambda *args, **kwargs: response)
    with pytest.raises((RuntimeError, ValueError)):
        network.join_devnet("ws://peer/v1/p2p", tmp_path)
    if existing:
        assert path.read_bytes() == original
    else:
        assert not path.exists()
    assert not path.with_name(path.name + ".tmp").exists()


def test_join_can_retry_after_invalid_response(tmp_path, monkeypatch):
    responses = iter([{**candidate(), "id": "00" * 32}, candidate()])
    def get(*args, **kwargs):
        return httpx.Response(200, json=next(responses), request=httpx.Request("GET", "http://peer/v1/genesis"))
    monkeypatch.setattr(network.httpx, "get", get)
    with pytest.raises(RuntimeError):
        network.join_devnet("ws://peer/v1/p2p", tmp_path)
    assert not network.has_devnet(tmp_path)
    network.join_devnet("ws://peer/v1/p2p", tmp_path)
    assert network.load_params("devnet", tmp_path, create=False).genesis_id.hex() == candidate()["id"]


def test_http_error_cannot_install_a_genesis(tmp_path, monkeypatch):
    response = httpx.Response(500, json=candidate(), request=httpx.Request("GET", "http://peer/v1/genesis"))
    monkeypatch.setattr(network.httpx, "get", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError, match="couldn.t fetch"):
        network.join_devnet("ws://peer/v1/p2p", tmp_path)
    assert not network.has_devnet(tmp_path)


@pytest.mark.parametrize("reply", [[], {"network": "devnet"}, {**candidate(), "message": None}])
def test_malformed_genesis_is_not_saved(tmp_path, monkeypatch, reply):
    response = httpx.Response(200, json=reply, request=httpx.Request("GET", "http://peer/v1/genesis"))
    monkeypatch.setattr(network.httpx, "get", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError):
        network.join_devnet("ws://peer/v1/p2p", tmp_path)
    assert not network.has_devnet(tmp_path)


def test_failed_join_reports_a_cli_error(tmp_path, monkeypatch, capsys):
    from talcash.cli import main
    def fail(*args, **kwargs):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(network.httpx, "get", fail)
    assert main(["--network", "devnet", "--datadir", str(tmp_path), "node", "--peer", "ws://peer/v1/p2p"]) == 1
    assert "couldn't fetch devnet genesis" in capsys.readouterr().err
    assert not network.has_devnet(tmp_path)
