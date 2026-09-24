"""The node's HTTP and WebSocket API, version 1. Interactive documentation at /docs.

    GET  /v1/status                      network, height, difficulty, fees
    GET  /v1/blocks/{height or id}       a block (?format=hex for raw bytes)
    GET  /v1/tx/{txid}                   a transaction: confirmed or waiting
    POST /v1/tx                          {"hex": ...} submit a signed transfer
    GET  /v1/mempool                     waiting transfers
    GET  /v1/address/{address}           balance, nonce, waiting and unlocking amounts
    GET  /v1/address/{address}/history   transactions, newest first
    GET  /v1/mining/template?address=    a block to mine
    POST /v1/mining/submit               {"hex": ...} a mined block
    GET  /v1/peers                       connected nodes
    GET  /v1/chunks/{index}              a sealed chunk file (one day of blocks), byte for byte
    GET  /v1/genesis                     the genesis block's parameters (devnet nodes join with it)
    WS   /v1/ws                          send {"subscribe": ["blocks", "mempool", "address:<addr>"]}
    WS   /v1/p2p                         node-to-node protocol (see p2p.py)

Errors are {"error": code, "detail": text} with a 4xx status.

Public mode (a node listening on a network, not just this computer): clients
other than this computer (and addresses given with --trust) are limited:
20 requests a second (bursts of 100, then 429 "rate-limited"), no mining
endpoints (403 "local-only"), lists of at most 100 items, and at most 4
WebSocket subscriptions per address. Behind a reverse proxy every client
would look local, so don't put a public node behind one.
"""

import asyncio
import contextlib
import itertools
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .. import __version__
from ..core.block import BlockHeader, block_from_bytes
from ..core.difficulty import difficulty
from ..core.errors import DecodeError, ValidationError
from ..core.tx import Coinbase, transaction_from_bytes
from ..crypto.address import decode_address, is_valid_address
from .limits import HostBuckets, is_loopback
from .p2p import P2PConfig, PeerManager
from .service import HistoryItem, NodeService
from .views import amount, block_view, tx_view

MAX_BODY_BYTES = 2_000_000
MAX_TOPICS = 1000


@dataclass(frozen=True)
class ApiPolicy:
    """How the API treats clients that aren't this computer (see "Public mode" above)."""

    public: bool = False
    trusted: frozenset[str] = frozenset()  # addresses with full access, e.g. a mining computer
    requests_per_second: float = 20
    burst: int = 100
    websockets_per_host: int = 4
    max_websockets: int = 1000
    stranger_topics: int = 100
    stranger_list_limit: int = 100

    def is_trusted(self, host: str | None) -> bool:
        return not self.public or host in self.trusted or is_loopback(host)


BackgroundTask = Callable[[NodeService], Awaitable[None]]


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = detail


class HexBody(BaseModel):
    hex: str


def create_app(
    open_service: Callable[[], NodeService],
    background: list[BackgroundTask] | None = None,
    p2p: P2PConfig | None = None,
    policy: ApiPolicy = ApiPolicy(),
) -> FastAPI:
    """`open_service` runs inside the server's event loop (SQLite connections belong to one thread).
    With `p2p`, the node also talks to other nodes at /v1/p2p."""
    requests = HostBuckets(policy.requests_per_second, policy.burst)
    websockets: Counter[str | None] = Counter()  # open /v1/ws connections of strangers, per address

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        service = open_service()
        app.state.service = service
        app.state.peers = PeerManager(service, p2p) if p2p is not None else None
        tasks = [asyncio.create_task(task(service)) for task in background or []]
        if app.state.peers is not None:
            tasks.append(asyncio.create_task(app.state.peers.run()))
        for task in tasks:
            task.add_done_callback(lambda t: _report_failure(t, service))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            service.close()

    app = FastAPI(title="TechnoCoin node", version=__version__, lifespan=lifespan,
                  description="Amounts are strings in TC with 6 decimals. Ids are hex.")

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse({"error": error.code, "detail": error.detail}, status_code=error.status)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return JSONResponse({"error": "too-large", "detail": f"at most {MAX_BODY_BYTES} bytes"}, status_code=413)
        host = request.client.host if request.client else None
        trusted = policy.is_trusted(host)
        request.state.trusted = trusted
        if not trusted:
            if not requests.try_take(str(host)):
                return JSONResponse({"error": "rate-limited", "detail": "too many requests, slow down"},
                                    status_code=429, headers={"Retry-After": "1"})
            if request.url.path.startswith("/v1/mining/"):
                return JSONResponse({"error": "local-only", "detail": "mining is only open to this computer"},
                                    status_code=403)
        return await call_next(request)

    def service_of(request: Request) -> NodeService:
        return request.app.state.service

    def list_limit(request: Request, limit: int) -> int:
        return limit if request.state.trusted else min(limit, policy.stranger_list_limit)

    def parse_address(service: NodeService, text: str) -> bytes:
        try:
            return decode_address(text, service.params.address_prefix)
        except ValueError as error:
            raise ApiError(400, "bad-address", str(error)) from None

    def parse_hex(text: str) -> bytes:
        try:
            return bytes.fromhex(text)
        except ValueError:
            raise ApiError(400, "bad-hex", "not a hex string") from None

    def item_view(service: NodeService, item: HistoryItem) -> dict:
        view = tx_view(item.tx, service.params)
        if item.location is None:
            return {**view, "status": "pending"}
        return {**view, "status": "confirmed", "height": item.location.height,
                "block": item.location.block_id.hex(), "time": item.time,
                "confirmations": service.confirmations(item.location.height)}

    @app.get("/v1/status")
    async def status(request: Request) -> dict:
        service = service_of(request)
        params, tip = service.params, service.chain.tip.header
        return {
            "network": params.name,
            "version": __version__,
            "genesis": service.chain.genesis.block_id.hex(),
            "height": tip.height,
            "tip": block_summary(service, tip),
            "finalized_height": service.chain.finalized_height(),
            "sealed_chunks": service.store.sealed_chunks(),
            "target_spacing": params.target_spacing,
            "block_reward": amount(params.block_reward),
            "coinbase_maturity": params.coinbase_maturity,
            "address_prefix": params.address_prefix,
            "min_fee_per_byte": amount(service.mempool.min_fee_per_byte),
            "mempool": {"count": len(service.mempool), "bytes": service.mempool.size_bytes},
            "peers": len(request.app.state.peers.ready_peers) if request.app.state.peers else 0,
        }

    @app.get("/v1/chunks/{index}")
    async def chunk_file(index: int, request: Request) -> Response:
        """A sealed chunk file, byte for byte (see blockfiles.py for the format)."""
        service = service_of(request)
        if not 0 <= index < service.store.sealed_chunks():
            raise ApiError(404, "unknown-chunk", "not sealed yet (or no such chunk)")
        path = service.store.files.chunk_path(index)
        if path is not None:
            return FileResponse(path, media_type="application/octet-stream", filename=path.name)
        return Response(service.store.files.chunk_bytes(index), media_type="application/octet-stream")

    @app.get("/v1/genesis")
    async def genesis(request: Request) -> dict:
        service = service_of(request)
        block = service.chain.genesis
        return {"network": service.params.name, "id": block.block_id.hex(), "timestamp": block.header.timestamp,
                "message": block.coinbase.memo.hex(), "nonce": block.header.nonce}

    @app.get("/v1/peers")
    async def peers(request: Request) -> list[dict]:
        manager = request.app.state.peers
        return manager.status() if manager else []

    @app.get("/v1/blocks/{ref}")
    async def get_block(ref: str, request: Request, format: str = "json") -> dict:
        service = service_of(request)
        if len(ref) < 64 and ref.isascii() and ref.isdigit():  # a height ("²".isdigit() is true, too)
            height = int(ref)
            block = service.chain.main_block(height) if height <= service.chain.tip_height else None
        else:
            block_id = parse_hex(ref)
            block = service.store.block(block_id) if len(block_id) == 32 else None
        if block is None:
            raise ApiError(404, "unknown-block")
        if format == "hex":
            return {"hex": block.serialize().hex()}
        on_main = service.chain.is_on_main_chain(block.block_id)
        return block_view(block, service.params, on_main_chain=on_main,
                          confirmations=service.confirmations(block.height) if on_main else 0)

    @app.get("/v1/tx/{txid}")
    async def get_tx(txid: str, request: Request) -> dict:
        service = service_of(request)
        item = service.find_transaction(parse_hex(txid))
        if item is None:
            raise ApiError(404, "unknown-transaction")
        return item_view(service, item)

    @app.post("/v1/tx")
    async def post_tx(body: HexBody, request: Request) -> dict:
        service = service_of(request)
        try:
            tx = transaction_from_bytes(parse_hex(body.hex))
        except DecodeError as error:
            raise ApiError(400, "bad-encoding", str(error)) from None
        try:
            service.submit_transaction(tx, origin=None)
        except ValidationError as error:
            raise ApiError(400, error.code, error.detail) from None
        return {"txid": tx.txid.hex(), "status": "pending"}

    @app.get("/v1/mempool")
    async def mempool(request: Request, limit: Annotated[int, Query(ge=1, le=10_000)] = 1000) -> dict:
        pool = service_of(request).mempool
        first = itertools.islice(pool.iter_entries(), list_limit(request, limit))
        return {"count": len(pool), "bytes": pool.size_bytes, "txids": [e.tx.txid.hex() for e in first]}

    @app.get("/v1/address/{address}")
    async def get_address(address: str, request: Request) -> dict:
        service = service_of(request)
        info = service.account(parse_address(service, address))
        return {
            "address": address,
            "balance": amount(info.balance),
            "available": amount(info.available),
            "pending_out": amount(info.pending_out),
            "pending_in": amount(info.pending_in),
            "immature": amount(info.immature),
            "nonce": info.nonce,
            "next_nonce": info.next_nonce,
        }

    @app.get("/v1/address/{address}/history")
    async def get_history(address: str, request: Request,
                          limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict]:
        service = service_of(request)
        payload = parse_address(service, address)
        result = []
        for item in service.history(payload, list_limit(request, limit)):
            tx = item.tx
            if isinstance(tx, Coinbase):
                kind, delta = "mined", tx.amount
            else:
                received = sum(o.amount for o in tx.outputs if o.address == payload)
                spent = tx.total_spent if tx.sender == payload else 0
                kind = "received" if not spent else ("self" if received else "sent")
                delta = received - spent
            result.append({"kind": kind, "amount": amount(delta), **item_view(service, item)})
        return result

    @app.get("/v1/mining/template")
    async def mining_template(address: str, request: Request) -> dict:
        service = service_of(request)
        block = service.template(parse_address(service, address))
        pow_params = service.params.pow
        return {
            "height": block.height,
            "hex": block.serialize().hex(),
            "target": f"{block.header.target:064x}",
            "pow": {"algorithm": "argon2id", "memory_kib": pow_params.memory_kib,
                    "iterations": pow_params.iterations, "salt": pow_params.salt.hex()},
        }

    @app.post("/v1/mining/submit")
    async def mining_submit(body: HexBody, request: Request) -> dict:
        service = service_of(request)
        data = parse_hex(body.hex)
        if len(data) > service.params.max_block_size:
            raise ApiError(400, "block-too-large")
        try:
            block = block_from_bytes(data)
        except DecodeError as error:
            raise ApiError(400, "bad-encoding", str(error)) from None
        result = service.submit_block(block, source="api")
        return {"id": block.block_id.hex(), "result": result.outcome.value,
                "error": result.error.code if result.error else None}

    @app.websocket("/v1/p2p")
    async def peer_to_peer(socket: WebSocket) -> None:
        manager = socket.app.state.peers
        if manager is None:
            await socket.close(code=1008)
            return
        await manager.accept(socket)

    @app.websocket("/v1/ws")
    async def websocket(socket: WebSocket) -> None:
        service: NodeService = socket.app.state.service
        host = socket.client.host if socket.client else None
        trusted = policy.is_trusted(host)
        if not trusted and (websockets[host] >= policy.websockets_per_host
                            or websockets.total() >= policy.max_websockets):
            await socket.close(code=1013)  # try again later
            return
        await socket.accept()
        subscription = service.events.subscribe()
        max_topics = MAX_TOPICS if trusted else policy.stranger_topics
        if not trusted:
            websockets[host] += 1

        async def read() -> None:
            while True:
                try:
                    message = await socket.receive_json()
                except (ValueError, TypeError, KeyError, RecursionError):  # not JSON text (binary, broken...)
                    subscription.deliver({"event": "error", "data": {"error": "bad-json"}})
                    continue
                if not trusted and not requests.try_take(str(host)):
                    subscription.deliver({"event": "error", "data": {"error": "rate-limited"}})
                    await asyncio.sleep(1)
                    continue
                topics = message.get("subscribe") if isinstance(message, dict) else None
                if not isinstance(topics, list):
                    subscription.deliver({"event": "error", "data": {"error": "expected {\"subscribe\": [...]}"}})
                    continue
                topics = topics[:MAX_TOPICS]
                service.events.add_topics(subscription, [t for t in topics if _valid_topic(t, service)], max_topics)
                subscription.deliver({"event": "subscribed", "data": {
                    "topics": sorted(subscription.topics),
                    "rejected": [t for t in topics if not (isinstance(t, str) and t in subscription.topics)]}})

        async def write() -> None:
            while not subscription.overflowed:
                await socket.send_json(await subscription.queue.get())

        tasks = {asyncio.create_task(read()), asyncio.create_task(write())}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            service.events.unsubscribe(subscription)
            if not trusted:
                websockets[host] -= 1
                if websockets[host] <= 0:
                    del websockets[host]
            with contextlib.suppress(Exception):
                await socket.close()

    return app


def block_summary(service: NodeService, header: BlockHeader) -> dict:
    return {"id": header.block_id.hex(), "height": header.height, "time": header.timestamp,
            "difficulty": difficulty(header.target, service.params)}


def _valid_topic(topic: object, service: NodeService) -> bool:
    if not isinstance(topic, str):
        return False
    if topic in ("blocks", "mempool"):
        return True
    return (isinstance(topic, str) and topic.startswith("address:")
            and is_valid_address(topic[len("address:"):], service.params.address_prefix))


def _report_failure(task: asyncio.Task, service: NodeService) -> None:
    if task.cancelled() or task.exception() is None:
        return
    error = task.exception()
    service.log("background task failed:\n" + "".join(traceback.format_exception(error)))
