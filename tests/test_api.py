from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from technocoin.core.block import Block, block_from_bytes
from technocoin.core.params import REGTEST
from technocoin.core.pow import meets_target, mine
from technocoin.crypto.address import encode_address
from technocoin.node.api import ApiPolicy, create_app
from technocoin.node.service import NodeService
from technocoin.node.store import Store

from chainutil import make_transfer, named_key

PARAMS = replace(REGTEST, coinbase_maturity=3)
ALICE, BOB = named_key("alice"), named_key("bob")
ALICE_TEXT = encode_address(ALICE.address, PARAMS.address_prefix)
BOB_TEXT = encode_address(BOB.address, PARAMS.address_prefix)


@pytest.fixture
def api():
    app = create_app(lambda: NodeService(PARAMS, Store(":memory:"), log=lambda line: None))
    with TestClient(app) as client:
        yield client


def mine_blocks(api: TestClient, address: str, count: int = 1) -> None:
    """Mine through the API, the way an external miner would."""
    for _ in range(count):
        template = api.get("/v1/mining/template", params={"address": address}).json()
        block = block_from_bytes(bytes.fromhex(template["hex"]))
        mined = Block(mine(block.header, PARAMS.pow), block.transactions)
        result = api.post("/v1/mining/submit", json={"hex": mined.serialize().hex()}).json()
        assert result["result"] == "new-tip", result


def test_status(api):
    status = api.get("/v1/status").json()
    assert status["network"] == "regtest" and status["height"] == 0
    assert status["block_reward"] == "10.000000"
    assert status["min_fee_per_byte"] == "0.000001"


def test_mining_and_balances(api):
    mine_blocks(api, ALICE_TEXT, 5)
    info = api.get(f"/v1/address/{ALICE_TEXT}").json()
    # Blocks 1-2 matured (maturity 3, tip 5); blocks 3-5 are still unlocking.
    assert info["balance"] == "20.000000" and info["available"] == "20.000000"
    assert info["immature"] == "30.000000"
    assert info["nonce"] == 0 and info["next_nonce"] == 0

    block = api.get("/v1/blocks/5").json()
    assert block["height"] == 5 and block["confirmations"] == 1 and block["on_main_chain"]
    assert block["transactions"][0]["to"] == ALICE_TEXT
    assert api.get(f"/v1/blocks/{block['id']}").json()["height"] == 5
    assert bytes.fromhex(api.get("/v1/blocks/5", params={"format": "hex"}).json()["hex"])


def test_send_confirm_and_history(api):
    mine_blocks(api, ALICE_TEXT, 4)
    tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 2_500_000)], fee=500, memo=b"lunch")
    posted = api.post("/v1/tx", json={"hex": tx.serialize().hex()})
    assert posted.status_code == 200 and posted.json() == {"txid": tx.txid.hex(), "status": "pending"}

    pending = api.get(f"/v1/tx/{tx.txid.hex()}").json()
    assert pending["status"] == "pending" and pending["memo"] == "lunch"
    alice = api.get(f"/v1/address/{ALICE_TEXT}").json()
    assert alice["pending_out"] == "2.500500" and alice["available"] == "7.499500" and alice["next_nonce"] == 1
    assert api.get(f"/v1/address/{BOB_TEXT}").json()["pending_in"] == "2.500000"
    assert api.get("/v1/mempool").json()["txids"] == [tx.txid.hex()]

    mine_blocks(api, ALICE_TEXT)
    confirmed = api.get(f"/v1/tx/{tx.txid.hex()}").json()
    assert confirmed["status"] == "confirmed" and confirmed["height"] == 5 and confirmed["confirmations"] == 1
    assert api.get(f"/v1/address/{BOB_TEXT}").json()["balance"] == "2.500000"

    [bob_item] = api.get(f"/v1/address/{BOB_TEXT}/history").json()
    assert bob_item["kind"] == "received" and bob_item["amount"] == "2.500000"
    alice_items = api.get(f"/v1/address/{ALICE_TEXT}/history").json()
    assert [item["height"] for item in alice_items] == [5, 5, 4, 3, 2, 1]  # newest block first
    assert {item["kind"] for item in alice_items[:2]} == {"sent", "mined"}
    sent = next(item for item in alice_items if item["txid"] == tx.txid.hex())
    assert sent["kind"] == "sent" and sent["amount"] == "-2.500500"


def test_errors(api):
    def error(response):
        return response.status_code, response.json()["error"]

    assert error(api.get("/v1/blocks/99")) == (404, "unknown-block")
    assert error(api.get("/v1/blocks/zz")) == (400, "bad-hex")
    assert error(api.get("/v1/tx/" + "00" * 32)) == (404, "unknown-transaction")
    assert error(api.get("/v1/address/tc1nope")) == (400, "bad-address")
    assert error(api.post("/v1/tx", json={"hex": "00"})) == (400, "bad-encoding")
    no_money = make_transfer(PARAMS, BOB, 0, [(ALICE.address, 1)], fee=500)
    assert error(api.post("/v1/tx", json={"hex": no_money.serialize().hex()})) == (400, "insufficient-funds")
    assert error(api.post("/v1/tx", content=b"x" * 3_000_000,
                          headers={"content-type": "application/json"})) == (413, "too-large")
    template = block_from_bytes(bytes.fromhex(api.get("/v1/mining/template", params={"address": ALICE_TEXT})
                                              .json()["hex"]))
    nonce = next(n for n in range(1000) if not meets_target(replace(template.header, nonce=n), PARAMS.pow))
    unmined = Block(replace(template.header, nonce=nonce), template.transactions)
    result = api.post("/v1/mining/submit", json={"hex": unmined.serialize().hex()}).json()
    assert (result["result"], result["error"]) == ("invalid", "bad-pow")


def test_websocket_events(api):
    with api.websocket_connect("/v1/ws") as socket:
        socket.send_json({"subscribe": ["blocks", f"address:{BOB_TEXT}", "address:nonsense", "weather"]})
        ack = socket.receive_json()
        assert ack["event"] == "subscribed"
        assert ack["data"]["topics"] == ["address:" + BOB_TEXT, "blocks"]
        assert ack["data"]["rejected"] == ["address:nonsense", "weather"]

        mine_blocks(api, ALICE_TEXT, 4)
        heights = [socket.receive_json()["data"]["height"] for _ in range(4)]
        assert heights == [1, 2, 3, 4]

        tx = make_transfer(PARAMS, ALICE, 0, [(BOB.address, 1_000_000)], fee=500)
        api.post("/v1/tx", json={"hex": tx.serialize().hex()})
        event = socket.receive_json()
        assert event["event"] == "address" and event["data"]["status"] == "pending"
        assert event["data"]["txid"] == tx.txid.hex()

        mine_blocks(api, ALICE_TEXT)
        events = [socket.receive_json(), socket.receive_json()]
        assert {e["event"] for e in events} == {"block", "address"}
        confirmed = next(e for e in events if e["event"] == "address")
        assert confirmed["data"]["status"] == "confirmed" and confirmed["data"]["height"] == 5

        socket.send_text("not json")
        assert socket.receive_json()["data"]["error"] == "bad-json"


def test_interactive_docs_are_served(api):
    assert api.get("/docs").status_code == 200
    assert "/v1/address/{address}" in api.get("/openapi.json").json()["paths"]


# --- public mode: what strangers get ---------------------------------------------------------
# TestClient requests come from "testclient", which isn't this computer: a stranger.


def public_api(**policy):
    app = create_app(lambda: NodeService(PARAMS, Store(":memory:"), log=lambda line: None),
                     policy=ApiPolicy(public=True, **policy))
    return TestClient(app)


def test_strangers_cannot_use_the_mining_endpoints():
    with public_api() as api:
        response = api.get("/v1/mining/template", params={"address": ALICE_TEXT})
        assert response.status_code == 403 and response.json()["error"] == "local-only"
        assert api.post("/v1/mining/submit", json={"hex": "00"}).status_code == 403
        assert api.get("/v1/status").status_code == 200  # everything else is open
    with public_api(trusted=frozenset({"testclient"})) as api:  # e.g. a mining computer on the network
        mine_blocks(api, ALICE_TEXT, 1)


def test_strangers_are_rate_limited():
    with public_api(requests_per_second=1, burst=5) as api:
        codes = [api.get("/v1/status").status_code for _ in range(8)]
        assert codes[:5] == [200] * 5 and set(codes[5:]) == {429}
        assert api.get("/v1/status").headers["Retry-After"] == "1"
    with TestClient(create_app(lambda: NodeService(PARAMS, Store(":memory:"), log=lambda line: None))) as api:
        assert {api.get("/v1/status").status_code for _ in range(300)} == {200}  # a private node isn't


def test_strangers_get_shorter_lists(tmp_path):
    def app(**policy):
        return create_app(lambda: NodeService(PARAMS, Store(tmp_path / "chain.sqlite"), log=lambda line: None),
                          policy=ApiPolicy(public=True, **policy))

    with TestClient(app(trusted=frozenset({"testclient"}))) as api:
        mine_blocks(api, ALICE_TEXT, 6)
        assert len(api.get(f"/v1/address/{ALICE_TEXT}/history", params={"limit": 50}).json()) == 6
    with TestClient(app(stranger_list_limit=2)) as api:  # the same node, seen by a stranger
        assert len(api.get(f"/v1/address/{ALICE_TEXT}/history", params={"limit": 50}).json()) == 2


def test_strangers_may_open_only_a_few_live_connections():
    with public_api(websockets_per_host=2) as api:
        with api.websocket_connect("/v1/ws") as first, api.websocket_connect("/v1/ws") as second:
            with pytest.raises(WebSocketDisconnect) as refused:
                with api.websocket_connect("/v1/ws") as third:
                    third.send_json({"subscribe": ["blocks"]})
                    third.receive_json()  # only reached if the third connection was let in
            assert refused.value.code == 1013
            first.send_json({"subscribe": ["blocks", "mempool"]})
            assert first.receive_json()["event"] == "subscribed"
        with api.websocket_connect("/v1/ws") as again:  # closed ones don't count any more
            again.send_json({"subscribe": ["blocks"]})
            assert again.receive_json()["event"] == "subscribed"
