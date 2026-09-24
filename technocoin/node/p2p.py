"""Peer-to-peer: how nodes find each other, pass on news, and catch up.

Nodes talk over WebSocket at /v1/p2p on the node's port, one JSON object per
message, each with a "type". Blocks, headers and transactions travel as hex.

  hello        first message both ways: protocol, network, genesis, node id, height, work, listen URL
  inv          "I have these": block and/or transaction ids
  get_data     "send me these": block and/or transaction ids
  block, tx    the data itself
  not_found    ids we were asked for but don't have
  get_headers  a block locator (our ids, dense near our tip, sparse further back)
  headers      up to 2,000 headers after the first locator block the peer also has
  get_peers    ask for addresses of other nodes
  peers        up to 100 node URLs

Spreading news: a new tip or accepted transfer is announced by id (inv) to every
peer that isn't known to have it; peers fetch only what they lack, so nothing
travels twice and nothing loops.

Catching up: when a peer has more total work than us (from its hello), or sends
a block whose parent we lack, we ask it for headers after our locator, download
those blocks 64 at a time in order, and hand them to the chain manager, which
switches branches if theirs has more work. One peer at a time; a peer that
stalls for 30 seconds is dropped and another is tried.

Misbehaviour (malformed messages, invalid blocks, wrong network) gets the peer
disconnected. Harmless disagreements (a clock slightly off, a fork deeper than
our finality) don't.
"""

import asyncio
import contextlib
import json
import os
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from .. import __version__
from ..core.block import Block, block_from_bytes, header_from_bytes
from ..core.errors import DecodeError, ValidationError
from ..core.tx import Transfer, transaction_from_bytes
from .chain import Outcome
from .service import NodeService

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024  # a full 1 MB block is 2 MB of hex
MAX_HEADERS = 2000
MAX_INV = 1000
MAX_LOCATOR = 101
MAX_PEER_URLS = 100
BLOCKS_PER_REQUEST = 64
HELLO_TIMEOUT = 10
STALL_TIMEOUT = 30
SEND_QUEUE_LIMIT = 5000
KNOWN_LIMIT = 20_000
CHECK_INTERVAL = 2

# Block rejections that don't mean the peer is misbehaving.
_HARMLESS_BLOCK_ERRORS = {"time-too-new", "fork-below-finality"}


class ProtocolError(Exception):
    """The peer broke the protocol; disconnect it."""


class _Recent:
    """A bounded set that forgets the oldest entries."""

    def __init__(self, limit: int = KNOWN_LIMIT) -> None:
        self._items: OrderedDict[bytes, None] = OrderedDict()
        self._limit = limit

    def add(self, item: bytes) -> None:
        self._items[item] = None
        self._items.move_to_end(item)
        if len(self._items) > self._limit:
            self._items.popitem(last=False)

    def __contains__(self, item: bytes) -> bool:
        return item in self._items


@dataclass
class P2PConfig:
    listen_url: str | None = None  # where others can reach us (advertised to peers)
    connect: list[str] = field(default_factory=list)  # nodes to always stay connected to
    target_outbound: int = 8
    max_inbound: int = 32


class Peer:
    def __init__(self, send_text: Callable, recv_text: Callable, close: Callable, *, outbound: bool, label: str):
        self._send_text, self.recv_text, self._close = send_text, recv_text, close
        self.outbound = outbound
        self.label = label
        self.url: str | None = label if outbound else None
        self.ready = False
        self.node_id = ""
        self.height = 0
        self.work = 0
        self.known_blocks = _Recent()
        self.known_txs = _Recent()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        # catching up from this peer
        self.waiting: deque[bytes] = deque()  # block ids still to request
        self.in_flight: set[bytes] = set()  # requested, not yet received
        self.more_headers = False
        self.last_header: bytes | None = None
        self.last_progress = time.monotonic()

    def send(self, message: dict) -> None:
        if self.closed:
            return
        if self.queue.qsize() >= SEND_QUEUE_LIMIT:
            self.closed = True  # too slow to keep up; the writer will close it
            self.queue.put_nowait(None)
            return
        self.queue.put_nowait(json.dumps(message, separators=(",", ":")))

    async def write_loop(self) -> None:
        try:
            while (text := await self.queue.get()) is not None:
                await self._send_text(text)
        except Exception:
            pass  # the connection went away; the reader notices too
        await self.close()  # also reached when the queue overflowed: the reader then stops

    async def close(self) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            await self._close()


def _ids(message: dict, key: str, limit: int) -> list[bytes]:
    values = message.get(key, [])
    if not isinstance(values, list) or len(values) > limit:
        raise ProtocolError(f"bad {key} list")
    try:
        ids = [bytes.fromhex(value) for value in values]
    except (TypeError, ValueError):
        raise ProtocolError(f"bad id in {key}") from None
    if any(len(i) != 32 for i in ids):
        raise ProtocolError(f"bad id in {key}")
    return ids


def _hex(message: dict, key: str) -> bytes:
    value = message.get(key)
    if not isinstance(value, str) or len(value) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"missing {key}")
    try:
        return bytes.fromhex(value)
    except ValueError:
        raise ProtocolError(f"bad hex in {key}") from None


def _valid_url(url: object) -> bool:
    return isinstance(url, str) and url.startswith(("ws://", "wss://")) and len(url) <= 200


class PeerManager:
    def __init__(self, service: NodeService, config: P2PConfig) -> None:
        self.service = service
        self.config = config
        self.node_id = os.urandom(16).hex()
        self.peers: set[Peer] = set()
        self.addresses: OrderedDict[str, None] = OrderedDict((url, None) for url in config.connect)
        self.self_urls: set[str] = set()
        self.url_node: dict[str, str] = {}  # address -> node id seen there (skip dialing nodes we already have)
        self.dialing: set[str] = set()
        self.retry_at: dict[str, float] = {}
        self.failures: dict[str, int] = {}
        self.sync_peer: Peer | None = None
        self.rejected_txs = _Recent()
        self._tasks: set[asyncio.Task] = set()
        if config.listen_url:
            self.self_urls.add(config.listen_url)
        service.tip_listeners.append(self._on_new_tip)
        service.tx_listeners.append(self._on_new_tx)

    def log(self, text: str) -> None:
        self.service.log(f"{time.strftime('%H:%M:%S')}  p2p: {text}")

    @property
    def ready_peers(self) -> list[Peer]:
        return [p for p in self.peers if p.ready and not p.closed]

    # --- running ------------------------------------------------------------

    async def run(self) -> None:
        """Keep outbound connections up and watch for stalled downloads. Runs until cancelled."""
        try:
            while True:
                self._dial_more()
                self._check_stall()
                await asyncio.sleep(CHECK_INTERVAL)
        finally:
            for task in list(self._tasks):
                task.cancel()
            for peer in list(self.peers):
                await peer.close()

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _dial_more(self) -> None:
        connected = {p.url for p in self.peers if p.url}
        connected_nodes = {p.node_id for p in self.ready_peers}
        outbound = sum(1 for p in self.peers if p.outbound) + len(self.dialing)
        now = time.monotonic()
        for url in list(self.addresses):
            if outbound >= self.config.target_outbound:
                break
            if (url in connected or url in self.dialing or url in self.self_urls
                    or self.url_node.get(url) in connected_nodes or self.retry_at.get(url, 0) > now):
                continue
            self.dialing.add(url)
            outbound += 1
            self._spawn(self._dial(url))

    async def _dial(self, url: str) -> None:
        try:
            try:
                connection = await ws_connect(url, max_size=MAX_MESSAGE_BYTES, open_timeout=5,
                                              ping_interval=20, ping_timeout=20)
            except (OSError, InvalidHandshake, InvalidURI, TimeoutError) as error:
                failures = self.failures.get(url, 0) + 1
                self.failures[url] = failures
                self.retry_at[url] = time.monotonic() + min(60, 2**failures)
                if failures == 1:
                    self.log(f"can't reach {url} ({type(error).__name__}); will keep trying")
                if failures >= 10 and url not in self.config.connect:
                    self.addresses.pop(url, None)  # a learned address that never works
                return
            self.failures.pop(url, None)

            async def recv_text() -> str:
                message = await connection.recv()
                if not isinstance(message, str):
                    raise ProtocolError("binary message")
                return message

            peer = Peer(connection.send, recv_text, connection.close, outbound=True, label=url)
        finally:
            self.dialing.discard(url)
        await self._serve(peer)

    async def accept(self, socket: WebSocket) -> None:
        """An incoming connection (called by the /v1/p2p route)."""
        if sum(1 for p in self.peers if not p.outbound) >= self.config.max_inbound:
            await socket.close(code=1013)  # try again later
            return
        await socket.accept()
        client = socket.client
        label = f"{client.host}:{client.port}" if client else "inbound"

        async def recv_text() -> str:
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("text") is None:
                raise ProtocolError("binary message")
            return message["text"]

        await self._serve(Peer(socket.send_text, recv_text, socket.close, outbound=False, label=label))

    async def _serve(self, peer: Peer) -> None:
        self.peers.add(peer)
        writer = asyncio.create_task(peer.write_loop())
        reason = "disconnected"
        try:
            peer.send(self._hello())
            self._handle_hello(peer, self._parse(await asyncio.wait_for(peer.recv_text(), HELLO_TIMEOUT)))
            while not peer.closed:
                self._handle(peer, self._parse(await peer.recv_text()))
        except ProtocolError as error:
            reason = f"dropped: {error}"
        except TimeoutError:
            reason = "dropped: no hello"
        except (ConnectionClosed, WebSocketDisconnect, OSError, RuntimeError):
            pass
        finally:
            writer.cancel()
            self.peers.discard(peer)
            await peer.close()
            if peer.ready:
                self.log(f"{peer.label} {reason}")
            if self.sync_peer is peer:
                self.sync_peer = None
                self._maybe_sync()

    def _parse(self, text: str) -> dict:
        try:
            message = json.loads(text)
        except (TypeError, ValueError):
            raise ProtocolError("not JSON") from None
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise ProtocolError("message without a type")
        return message

    # --- handshake ----------------------------------------------------------

    def _hello(self) -> dict:
        tip = self.service.chain.tip
        return {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "network": self.service.params.name,
            "genesis": self.service.chain.genesis.block_id.hex(),
            "node_id": self.node_id,
            "height": tip.height,
            "work": f"{tip.chain_work:x}",
            "listen": self.config.listen_url,
            "agent": f"technocoin/{__version__}",
        }

    def _handle_hello(self, peer: Peer, message: dict) -> None:
        if message.get("type") != "hello":
            raise ProtocolError("expected hello")
        if message.get("protocol") != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol {message.get('protocol')!r}")
        if message.get("network") != self.service.params.name or \
                message.get("genesis") != self.service.chain.genesis.block_id.hex():
            raise ProtocolError("different network")
        node_id = message.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ProtocolError("missing node id")
        if node_id == self.node_id:
            if peer.url:
                self.self_urls.add(peer.url)
            peer.closed = True  # connected to ourselves
            return
        if peer.url:
            self.url_node[peer.url] = node_id
        if _valid_url(message.get("listen")):
            self.url_node[message["listen"]] = node_id
        if any(p.node_id == node_id for p in self.ready_peers):
            peer.closed = True  # already connected to this node
            return
        try:
            peer.height = int(message.get("height", 0))
            peer.work = int(message.get("work", "0"), 16)
        except (TypeError, ValueError):
            raise ProtocolError("bad height or work") from None
        peer.node_id = node_id
        peer.ready = True
        if _valid_url(message.get("listen")) and not peer.outbound:
            self._learn(message["listen"])
        self.log(f"connected to {peer.label} ({'outbound' if peer.outbound else 'inbound'}, height {peer.height})")

        peer.send({"type": "get_peers"})
        txids = [e.tx.txid.hex() for e in self.service.mempool.entries()]
        for start in range(0, len(txids), MAX_INV):
            peer.send({"type": "inv", "txs": txids[start:start + MAX_INV]})
        self._maybe_sync()

    def _learn(self, url: str) -> None:
        if url not in self.addresses and url not in self.self_urls and len(self.addresses) < 1000:
            self.addresses[url] = None

    # --- messages -------------------------------------------------------------

    def _handle(self, peer: Peer, message: dict) -> None:
        kind = message["type"]
        handler = {
            "inv": self._on_inv,
            "get_data": self._on_get_data,
            "block": self._on_block,
            "tx": self._on_tx,
            "not_found": self._on_not_found,
            "get_headers": self._on_get_headers,
            "headers": self._on_headers,
            "get_peers": self._on_get_peers,
            "peers": self._on_peers,
        }.get(kind)
        if handler is None:
            raise ProtocolError(f"unknown message {kind!r}")
        handler(peer, message)

    def _on_inv(self, peer: Peer, message: dict) -> None:
        blocks, txs = _ids(message, "blocks", MAX_INV), _ids(message, "txs", MAX_INV)
        for block_id in blocks:
            peer.known_blocks.add(block_id)
        for txid in txs:
            peer.known_txs.add(txid)
        store, mempool = self.service.store, self.service.mempool
        want_blocks = [b.hex() for b in blocks if store.header(b) is None]
        want_txs = [t.hex() for t in txs
                    if t not in mempool and t not in self.rejected_txs and store.find_tx(t) is None]
        if want_blocks or want_txs:
            peer.send({"type": "get_data", "blocks": want_blocks, "txs": want_txs})

    def _on_get_data(self, peer: Peer, message: dict) -> None:
        missing_blocks, missing_txs = [], []
        for block_id in _ids(message, "blocks", MAX_INV):
            block = self.service.store.block(block_id)
            if block is None:
                missing_blocks.append(block_id.hex())
            else:
                peer.known_blocks.add(block_id)
                peer.send({"type": "block", "hex": block.serialize().hex()})
        for txid in _ids(message, "txs", MAX_INV):
            entry = self.service.mempool.get(txid)
            if entry is None:
                missing_txs.append(txid.hex())
            else:
                peer.known_txs.add(txid)
                peer.send({"type": "tx", "hex": entry.tx.serialize().hex()})
        if missing_blocks or missing_txs:
            peer.send({"type": "not_found", "blocks": missing_blocks, "txs": missing_txs})

    def _on_block(self, peer: Peer, message: dict) -> None:
        data = _hex(message, "hex")
        if len(data) > self.service.params.max_block_size:
            raise ProtocolError("block too large")
        try:
            block = block_from_bytes(data)
        except DecodeError as error:
            raise ProtocolError(f"unreadable block: {error}") from None
        block_id = block.block_id
        peer.known_blocks.add(block_id)
        peer.height = max(peer.height, block.height)
        syncing = block_id in peer.in_flight
        result = self.service.submit_block(block, source=f"from {peer.label}", origin=peer, quiet=syncing)
        if result.outcome is Outcome.INVALID:
            if result.error and result.error.code in _HARMLESS_BLOCK_ERRORS:
                peer.work = 0  # don't try to sync from it
            else:
                raise ProtocolError(f"sent an invalid block ({result.error.code if result.error else '?'})")
        elif result.outcome is Outcome.ORPHAN and self.sync_peer is None:
            self._start_sync(peer)  # we're missing its parent: catch up from this peer
        if syncing:
            peer.in_flight.discard(block_id)
            peer.last_progress = time.monotonic()
            self._continue_sync(peer)

    def _on_tx(self, peer: Peer, message: dict) -> None:
        try:
            tx = transaction_from_bytes(_hex(message, "hex"))
        except DecodeError as error:
            raise ProtocolError(f"unreadable transaction: {error}") from None
        peer.known_txs.add(tx.txid)
        if tx.txid in self.service.mempool or tx.txid in self.rejected_txs:
            return
        try:
            self.service.submit_transaction(tx, origin=peer)
        except ValidationError:
            self.rejected_txs.add(tx.txid)  # already mined, conflicting, fee too low... not the peer's fault

    def _on_not_found(self, peer: Peer, message: dict) -> None:
        for block_id in _ids(message, "blocks", MAX_INV):
            if block_id in peer.in_flight:
                raise ProtocolError("announced a block it doesn't have")

    def _on_get_headers(self, peer: Peer, message: dict) -> None:
        locator = _ids(message, "locator", MAX_LOCATOR)
        headers = self.service.chain.headers_after(locator, MAX_HEADERS)
        peer.send({"type": "headers", "headers": [h.serialize().hex() for h in headers]})

    def _on_get_peers(self, peer: Peer, message: dict) -> None:
        urls = [url for url in self.addresses if url not in self.self_urls][:MAX_PEER_URLS - 1]
        if self.config.listen_url:
            urls.append(self.config.listen_url)
        peer.send({"type": "peers", "urls": urls})

    def _on_peers(self, peer: Peer, message: dict) -> None:
        urls = message.get("urls", [])
        if not isinstance(urls, list) or len(urls) > MAX_PEER_URLS:
            raise ProtocolError("bad peers list")
        for url in urls:
            if _valid_url(url):
                self._learn(url)

    # --- catching up ----------------------------------------------------------

    def _maybe_sync(self) -> None:
        if self.sync_peer is not None:
            return
        ours = self.service.chain.tip.chain_work
        candidates = [p for p in self.ready_peers if p.work > ours]
        if candidates:
            self._start_sync(max(candidates, key=lambda p: p.work))

    def _start_sync(self, peer: Peer) -> None:
        self.sync_peer = peer
        peer.last_progress = time.monotonic()
        self.log(f"catching up from {peer.label} (our height {self.service.chain.tip_height})")
        peer.send({"type": "get_headers", "locator": [i.hex() for i in self.service.chain.locator()]})

    def _on_headers(self, peer: Peer, message: dict) -> None:
        values = message.get("headers", [])
        if not isinstance(values, list) or len(values) > MAX_HEADERS:
            raise ProtocolError("bad headers list")
        if peer is not self.sync_peer:
            return  # we didn't ask
        try:
            headers = [header_from_bytes(bytes.fromhex(v)) for v in values]
        except (TypeError, ValueError, DecodeError):
            raise ProtocolError("unreadable header") from None
        for earlier, later in zip(headers, headers[1:]):
            if later.prev_id != earlier.block_id:
                raise ProtocolError("headers don't form a chain")
        peer.last_progress = time.monotonic()
        peer.more_headers = len(headers) == MAX_HEADERS
        peer.last_header = headers[-1].block_id if headers else None
        store = self.service.store
        peer.waiting.extend(h.block_id for h in headers if store.header(h.block_id) is None)
        self._continue_sync(peer)

    def _continue_sync(self, peer: Peer) -> None:
        if peer is not self.sync_peer or peer.in_flight:
            return
        if peer.waiting:
            batch = [peer.waiting.popleft() for _ in range(min(BLOCKS_PER_REQUEST, len(peer.waiting)))]
            peer.in_flight.update(batch)
            peer.send({"type": "get_data", "blocks": [b.hex() for b in batch], "txs": []})
        elif peer.more_headers and peer.last_header is not None:
            locator = [peer.last_header] + self.service.chain.locator()[:MAX_LOCATOR - 1]
            peer.send({"type": "get_headers", "locator": [i.hex() for i in locator]})
        else:
            tip = self.service.chain.tip
            self.log(f"caught up with {peer.label} at height {tip.height}")
            peer.work = min(peer.work, tip.chain_work)
            self.sync_peer = None
            self._maybe_sync()

    def _check_stall(self) -> None:
        peer = self.sync_peer
        if peer is not None and time.monotonic() - peer.last_progress > STALL_TIMEOUT:
            self.log(f"{peer.label} stopped sending blocks; trying another peer")
            peer.work = 0
            self.sync_peer = None
            self._spawn(peer.close())
            self._maybe_sync()

    # --- passing news on ----------------------------------------------------

    def _on_new_tip(self, block: Block, origin: object) -> None:
        for peer in self.ready_peers:
            if peer is not origin and block.block_id not in peer.known_blocks:
                peer.known_blocks.add(block.block_id)
                peer.send({"type": "inv", "blocks": [block.block_id.hex()], "txs": []})

    def _on_new_tx(self, tx: Transfer, origin: object) -> None:
        for peer in self.ready_peers:
            if peer is not origin and tx.txid not in peer.known_txs:
                peer.known_txs.add(tx.txid)
                peer.send({"type": "inv", "blocks": [], "txs": [tx.txid.hex()]})

    def status(self) -> list[dict]:
        return [{"peer": p.label, "outbound": p.outbound, "height": p.height,
                 "syncing": p is self.sync_peer} for p in sorted(self.ready_peers, key=lambda p: p.label)]
