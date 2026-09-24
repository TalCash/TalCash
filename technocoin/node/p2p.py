"""Peer-to-peer: how nodes find each other, pass on news, and catch up.

Nodes talk over WebSocket at /v1/p2p on the node's port, one JSON object per
message, each with a "type". Blocks, headers and transactions travel as hex.

  hello        first message both ways: protocol, network, genesis, node id, height, work,
               sealed chunks, listen URL
  inv          "I have these": block and/or transaction ids
  get_data     "send me these": block and/or transaction ids
  block, tx    the data itself
  not_found    ids we were asked for but don't have
  get_headers  a block locator (our ids, dense near our tip, sparse further back)
  headers      up to 2,000 headers after the first locator block the peer also has
  get_peers    ask for addresses of other nodes
  peers        up to 100 node URLs

Unknown message types are ignored, so later versions can add messages.

Spreading news: a new tip or accepted transfer is announced by id (inv) to every
peer that isn't known to have it; peers fetch only what they lack, so nothing
travels twice and nothing loops.

Catching up: when a peer has more total work than us (from its hello), or sends
a block whose parent we lack, we catch up from it. First, whole sealed days:
if its hello says it has sealed chunks we don't, we download those chunk files
over HTTP (GET /v1/chunks/{i} on the same port) and import each in one go
(checked in a background thread, then applied in one database transaction).
Then the rest, headers first: we ask for headers after our locator and check
every one of them (links, height, timestamps, difficulty, and proof of work on
all cores) before downloading any block. Only a chain whose checked headers add
up to more work than ours gets downloaded, 64 blocks at a time, in order; the
chain manager switches branches once the downloaded blocks have more work. One
peer at a time; a peer that stalls for 30 seconds is dropped and another is
tried. A chunk that doesn't fit our chain (we're on a different branch) or
can't be downloaded just means falling back to headers.

Limits: every message costs a peer some of its budget (500 units a second,
saving up to 5,000; asking for headers or blocks costs more than announcing).
A peer over budget isn't disconnected, just read more slowly: we stop reading
from it until it's back within budget, so flooding only slows the flooder down.
Blocks a peer asks for are read from disk one at a time as they're sent, so
requests can't fill our memory. At most 4 inbound connections per IP address
(not counting this computer).

Misbehaviour: anything an honest node never does (malformed messages, invalid
blocks, headers or transfers, damaged chunk files) gets the peer disconnected
and banned for an hour, by node id and by IP address (a node on this computer
only by node id). Harmless disagreements (a clock slightly off, a fork deeper
than our finality, a transfer that's only invalid because of the order things
arrived in) don't count.

Addresses of nodes we managed to connect to are saved (peers.json) so a
restarted node finds the network again without being told.
"""

import asyncio
import contextlib
import json
import os
import time
import traceback
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from .. import __version__
from ..core.block import Block, block_from_bytes, header_from_bytes
from ..core.errors import DecodeError, ValidationError
from ..core.tx import Transfer, transaction_from_bytes
from .blockfiles import ChunkError
from .chain import HeaderTip, Outcome, all_meet_target, check_chunk_file
from .limits import TokenBucket, is_loopback
from .service import NodeService
from .store import STATUS_INVALID, STATUS_VALID

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
SAVE_PEERS_INTERVAL = 30
BAN_SECONDS = 3600
MAX_INBOUND_PER_HOST = 4
MAX_PENDING_HEADERS = 50_000  # checked headers waiting for their blocks, per sync

# Each peer's message budget (see "Limits" above). Messages not listed cost 1.
MESSAGE_RATE = 500
MESSAGE_BURST = 5000
MESSAGE_COSTS = {"get_headers": 250, "get_peers": 50, "peers": 5, "block": 5, "tx": 2}

# Block rejections that don't mean the peer is misbehaving.
_HARMLESS_BLOCK_ERRORS = {"time-too-new", "fork-below-finality"}
# Transfer rejections that do: no honest node relays a transfer that breaks these rules,
# because they don't depend on balances or on what else is waiting.
_INVALID_TX_ERRORS = {"wrong-network", "memo-too-large", "bad-output-count", "bad-address", "bad-amount",
                      "bad-fee", "amount-overflow", "bad-signature", "not-a-transfer"}


class ProtocolError(Exception):
    """The peer broke the protocol: disconnect it, and with `ban`, refuse it for an hour."""

    def __init__(self, reason: str, *, ban: bool = True) -> None:
        super().__init__(reason)
        self.ban = ban


def _http_base(ws_url: str | None) -> str | None:
    """ws://host:port/v1/p2p -> http://host:port (a node's API is on the same port)."""
    if not ws_url or not ws_url.startswith(("ws://", "wss://")):
        return None
    return "http" + ws_url[2:].split("/v1/")[0]


def _url_host(url: str | None) -> str | None:
    try:
        return urlsplit(url).hostname if url else None
    except ValueError:
        return None


def max_chunk_file_bytes(params) -> int:
    """The largest a chunk file can honestly be: a day of full blocks plus the file's own overhead."""
    return params.chunk_size * (params.max_block_size + 64) + 1_000_000


async def download_chunk(http_base: str, index: int, max_bytes: int) -> bytes | None:
    """A peer's sealed chunk file, or None if it doesn't have it. Refuses files over `max_bytes`."""
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream("GET", f"{http_base}/v1/chunks/{index}") as response:
            if response.status_code != 200:
                return None
            parts, total = [], 0
            async for part in response.aiter_bytes():
                total += len(part)
                if total > max_bytes:
                    raise ProtocolError("chunk file too large")
                parts.append(part)
            return b"".join(parts)


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
    peers_file: Path | None = None  # remember working addresses here across restarts


class Peer:
    def __init__(self, send_text: Callable, recv_text: Callable, close: Callable, *, outbound: bool, label: str,
                 host: str | None = None):
        self._send_text, self.recv_text, self._close = send_text, recv_text, close
        self.outbound = outbound
        self.label = label
        self.url: str | None = label if outbound else None
        self.host = host  # IP address (or name, for outbound) for bans and per-address limits
        self.ready = False
        self.node_id = ""
        self.height = 0
        self.work = 0
        self.chunks = 0  # sealed chunks the peer can serve
        self.http_url: str | None = None  # the peer's API (for chunk downloads)
        self.known_blocks = _Recent()
        self.known_txs = _Recent()
        self.budget = TokenBucket(MESSAGE_RATE, MESSAGE_BURST)
        # Messages to send: text, or a function making the text when its turn comes (big blocks
        # are only read from disk then, so a long request list can't fill our memory).
        self.queue: asyncio.Queue[str | Callable[[], str | None] | None] = asyncio.Queue()
        self.closed = False
        self.drop_reason: str | None = None
        # catching up from this peer
        self.sync_round = 0  # bumped at every new sync, so late results of an old one are ignored
        self.header_tip: HeaderTip | None = None  # end of the peer's chain as far as we've checked its headers
        self.waiting: deque[bytes] = deque()  # blocks (with checked headers) still to request
        self.in_flight: set[bytes] = set()  # requested, not yet received
        self.more_headers = False
        self.awaiting_headers = False  # asked for headers; nothing else may arrive as "headers"
        self.checking_headers = False
        self.last_progress = time.monotonic()

    def send(self, message: dict) -> None:
        self._put(json.dumps(message, separators=(",", ":")))

    def send_later(self, make: Callable[[], str | None]) -> None:
        self._put(make)

    def _put(self, item) -> None:
        if self.closed:
            return
        if self.queue.qsize() >= SEND_QUEUE_LIMIT:
            self.closed = True  # too slow to keep up; the writer will close it
            self.queue.put_nowait(None)
            return
        self.queue.put_nowait(item)

    async def write_loop(self) -> None:
        try:
            while (item := await self.queue.get()) is not None:
                text = item() if callable(item) else item
                if text is not None:
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


def _count(message: dict, key: str) -> int:
    value = message.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**63:
        raise ProtocolError(f"bad {key}")
    return value


def _valid_url(url: object) -> bool:
    return isinstance(url, str) and url.startswith(("ws://", "wss://")) and len(url) <= 200


def _length(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _cost(message: dict) -> float:
    """How much of a peer's budget a message uses."""
    kind = message["type"]
    cost = MESSAGE_COSTS.get(kind, 1)
    if kind == "get_data":  # serving blocks is the expensive part
        cost += 2 * _length(message.get("blocks")) + _length(message.get("txs")) / 10
    elif kind == "inv":
        cost += (_length(message.get("blocks")) + _length(message.get("txs"))) / 100
    return cost


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
        self.banned: dict[str, float] = {}  # node id -> refused until (monotonic time)
        self.banned_hosts: dict[str, float] = {}  # IP address -> refused until (never this computer)
        self.rejected_txs = _Recent()
        self._tasks: set[asyncio.Task] = set()
        if config.listen_url:
            self.self_urls.add(config.listen_url)
        self.remembered: set[str] = set()
        self._remembered_changed = False
        self._load_peers()
        service.tip_listeners.append(self._on_new_tip)
        service.tx_listeners.append(self._on_new_tx)

    # --- remembering peers across restarts ---------------------------------------

    def _load_peers(self) -> None:
        path = self.config.peers_file
        if path is None or not path.exists():
            return
        try:
            urls = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # a damaged peers file isn't worth failing over
        for url in urls if isinstance(urls, list) else []:
            if _valid_url(url) and url not in self.self_urls:
                self.remembered.add(url)
                self.addresses.setdefault(url, None)

    def _remember(self, url: str) -> None:
        if url not in self.remembered and url not in self.self_urls:
            self.remembered.add(url)
            self._remembered_changed = True

    def _save_peers(self) -> None:
        path = self.config.peers_file
        if path is None or not self._remembered_changed:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(sorted(self.remembered)[:1000], indent=1), encoding="utf-8")
        os.replace(temporary, path)
        self._remembered_changed = False

    def log(self, text: str) -> None:
        self.service.log(f"{time.strftime('%H:%M:%S')}  p2p: {text}")

    @property
    def ready_peers(self) -> list[Peer]:
        return [p for p in self.peers if p.ready and not p.closed]

    # --- running ------------------------------------------------------------

    async def run(self) -> None:
        """Keep outbound connections up and watch for stalled downloads. Runs until cancelled."""
        last_save = time.monotonic()
        try:
            while True:
                self._forget_expired_bans()
                self._dial_more()
                self._check_stall()
                if time.monotonic() - last_save > SAVE_PEERS_INTERVAL:
                    self._save_peers()
                    last_save = time.monotonic()
                await asyncio.sleep(CHECK_INTERVAL)
        finally:
            self._save_peers()
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
                    or self.url_node.get(url) in connected_nodes or self.retry_at.get(url, 0) > now
                    or self.banned_hosts.get(_url_host(url) or "", 0) > now):
                continue
            self.dialing.add(url)
            outbound += 1
            self._spawn(self._dial(url))

    async def _dial(self, url: str) -> None:
        try:
            try:
                connection = await ws_connect(url, max_size=MAX_MESSAGE_BYTES, open_timeout=5,
                                              ping_interval=20, ping_timeout=20)
            except (OSError, InvalidHandshake, InvalidURI, TimeoutError, ValueError) as error:
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

            remote = connection.remote_address
            host = remote[0] if isinstance(remote, tuple) and remote else _url_host(url)
            peer = Peer(connection.send, recv_text, connection.close, outbound=True, label=url, host=host)
        finally:
            self.dialing.discard(url)
        await self._serve(peer)

    def inbound_refusal(self, host: str | None) -> int | None:
        """Why an incoming connection from `host` must be refused (a WebSocket close code), or None."""
        inbound = [p for p in self.peers if not p.outbound]
        if len(inbound) >= self.config.max_inbound:
            return 1013  # try again later
        if host and self.banned_hosts.get(host, 0) > time.monotonic():
            return 1008  # policy violation
        if host and not is_loopback(host) and sum(p.host == host for p in inbound) >= MAX_INBOUND_PER_HOST:
            return 1013
        return None

    async def accept(self, socket: WebSocket) -> None:
        """An incoming connection (called by the /v1/p2p route)."""
        client = socket.client
        host = client.host if client else None
        refusal = self.inbound_refusal(host)
        if refusal is not None:
            await socket.close(code=refusal)
            return
        await socket.accept()
        label = f"{client.host}:{client.port}" if client else "inbound"

        async def recv_text() -> str:
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("text") is None:
                raise ProtocolError("binary message")
            return message["text"]

        await self._serve(Peer(socket.send_text, recv_text, socket.close, outbound=False, label=label, host=host))

    async def _serve(self, peer: Peer) -> None:
        self.peers.add(peer)
        writer = asyncio.create_task(peer.write_loop())
        reason = "disconnected"
        try:
            peer.send(self._hello())
            self._handle_hello(peer, self._parse(await asyncio.wait_for(peer.recv_text(), HELLO_TIMEOUT)))
            while not peer.closed:
                message = self._parse(await peer.recv_text())
                wait = peer.budget.take(_cost(message))
                if wait:
                    await asyncio.sleep(wait)  # over budget: we stop reading from it meanwhile
                if not peer.closed:
                    self._handle(peer, message)
        except ProtocolError as error:
            if error.ban:
                self._ban(peer)
                reason = f"banned for {BAN_SECONDS // 60} minutes: {error}"
            else:
                reason = f"dropped: {error}"
        except TimeoutError:
            reason = "dropped: no hello"
        except (ConnectionClosed, WebSocketDisconnect, OSError, RuntimeError):
            pass
        except Exception as error:  # a bug on our side: report it, drop the peer, keep running
            reason = f"dropped after an internal error ({error!r})"
            self.service.log("".join(traceback.format_exception(error)))
        finally:
            writer.cancel()
            self.peers.discard(peer)
            await peer.close()
            reason = peer.drop_reason or reason
            if peer.ready or reason.startswith("banned"):
                self.log(f"{peer.label} {reason}")
            if self.sync_peer is peer:
                self.sync_peer = None
                self._maybe_sync()

    def _parse(self, text: str) -> dict:
        try:
            message = json.loads(text)
        except (TypeError, ValueError, RecursionError):
            raise ProtocolError("not JSON") from None
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise ProtocolError("message without a type")
        return message

    # --- misbehaviour -----------------------------------------------------------

    def _ban(self, peer: Peer) -> None:
        until = time.monotonic() + BAN_SECONDS
        if peer.node_id:
            self.banned[peer.node_id] = until  # refused when it connects to us, whatever its address
        if peer.url:
            self.retry_at[peer.url] = until
        if peer.host and not is_loopback(peer.host):
            self.banned_hosts[peer.host] = until  # new node ids from the same address don't help it
        peer.work = 0

    def _drop(self, peer: Peer, reason: str, *, ban: bool) -> None:
        """Disconnect a peer from outside its reader (e.g. after a background check failed)."""
        if ban:
            self._ban(peer)
        peer.drop_reason = f"banned for {BAN_SECONDS // 60} minutes: {reason}" if ban else f"dropped: {reason}"
        peer.work = 0
        peer.closed = True  # at once: nothing more gets sent to it or taken from it
        if self.sync_peer is peer:
            self.sync_peer = None
        self._spawn(peer.close())
        self._maybe_sync()

    def _forget_expired_bans(self) -> None:
        now = time.monotonic()
        for table in (self.banned, self.banned_hosts):
            for key in [key for key, until in table.items() if until <= now]:
                del table[key]

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
            "chunks": self.service.store.sealed_chunks(),
            "listen": self.config.listen_url,
            "agent": f"technocoin/{__version__}",
        }

    def _handle_hello(self, peer: Peer, message: dict) -> None:
        if message.get("type") != "hello":
            raise ProtocolError("expected hello", ban=False)
        if message.get("protocol") != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol {str(message.get('protocol'))[:20]!r}", ban=False)
        if message.get("network") != self.service.params.name or \
                message.get("genesis") != self.service.chain.genesis.block_id.hex():
            raise ProtocolError("different network", ban=False)
        node_id = message.get("node_id")
        if not isinstance(node_id, str) or not 1 <= len(node_id) <= 64:
            raise ProtocolError("missing node id")
        if node_id == self.node_id:
            if peer.url:
                self.self_urls.add(peer.url)
            peer.closed = True  # connected to ourselves
            return
        if self.banned.get(node_id, 0) > time.monotonic():
            raise ProtocolError("banned for misbehaving earlier", ban=False)
        height, chunks = _count(message, "height"), _count(message, "chunks")
        work = message.get("work", "0")
        try:
            if not isinstance(work, str) or len(work) > 80:
                raise ValueError
            work = int(work, 16)
        except ValueError:
            raise ProtocolError("bad work") from None
        if peer.url:
            self.url_node[peer.url] = node_id
        listen = message.get("listen") if _valid_url(message.get("listen")) else None
        if listen:
            self.url_node[listen] = node_id
        if any(p.node_id == node_id for p in self.ready_peers):
            peer.closed = True  # already connected to this node
            return
        peer.height, peer.work, peer.chunks = height, work, chunks
        peer.node_id = node_id
        peer.ready = True
        peer.http_url = _http_base(peer.url if peer.outbound else listen)
        if peer.outbound:
            self._remember(peer.url)
        elif listen:
            self._learn(listen)
        self.log(f"connected to {peer.label} ({'outbound' if peer.outbound else 'inbound'}, height {peer.height})")

        peer.send({"type": "get_peers"})
        txids = [e.tx.txid.hex() for e in self.service.mempool.iter_entries()]
        for start in range(0, len(txids), MAX_INV):
            peer.send({"type": "inv", "txs": txids[start:start + MAX_INV]})
        self._maybe_sync()

    def _learn(self, url: str) -> None:
        if url not in self.addresses and url not in self.self_urls and len(self.addresses) < 1000:
            self.addresses[url] = None

    # --- messages -------------------------------------------------------------

    def _handle(self, peer: Peer, message: dict) -> None:
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
        }.get(message["type"])
        if handler is not None:  # unknown types are ignored (see the module docstring)
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
        store = self.service.store
        missing_blocks, missing_txs = [], []
        for block_id in _ids(message, "blocks", MAX_INV):
            stored = store.header(block_id)
            if stored is None or stored.status != STATUS_VALID:
                missing_blocks.append(block_id.hex())  # never pass on a block we haven't fully checked
            else:
                peer.known_blocks.add(block_id)
                peer.send_later(partial(self._block_message, block_id))
        for txid in _ids(message, "txs", MAX_INV):
            entry = self.service.mempool.get(txid)
            if entry is None:
                missing_txs.append(txid.hex())
            else:
                peer.known_txs.add(txid)
                peer.send({"type": "tx", "hex": entry.tx.serialize().hex()})
        if missing_blocks or missing_txs:
            peer.send({"type": "not_found", "blocks": missing_blocks, "txs": missing_txs})

    def _block_message(self, block_id: bytes) -> str | None:
        data = self.service.store.block_bytes(block_id)
        return None if data is None else json.dumps({"type": "block", "hex": data.hex()}, separators=(",", ":"))

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
        syncing = block_id in peer.in_flight
        if not syncing and self.service.store.header(block.header.prev_id) is None:
            # We lack its parent: the peer is ahead of us. Catch up from it, headers first,
            # instead of holding on to a block we can't check yet.
            peer.work = max(peer.work, self.service.chain.tip.chain_work + 1)
            if self.sync_peer is None:
                self._start_sync(peer)
            return
        peer.height = max(peer.height, block.height)
        result = self.service.submit_block(block, source=f"from {peer.label}", origin=peer, quiet=syncing)
        if result.outcome is Outcome.INVALID:
            if result.error and result.error.code in _HARMLESS_BLOCK_ERRORS:
                peer.work = 0  # don't try to sync from it
            else:
                raise ProtocolError(f"sent an invalid block ({result.error.code if result.error else '?'})")
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
        except ValidationError as error:
            if error.code in _INVALID_TX_ERRORS:
                raise ProtocolError(f"sent an invalid transfer ({error.code})") from None
            self.rejected_txs.add(tx.txid)  # already mined, conflicting, fee too low... not the peer's fault

    def _on_not_found(self, peer: Peer, message: dict) -> None:
        for block_id in _ids(message, "blocks", MAX_INV):
            if block_id in peer.in_flight:
                raise ProtocolError("didn't send a block it announced", ban=False)

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
        peer.sync_round += 1
        peer.header_tip, peer.more_headers = None, False
        peer.awaiting_headers = peer.checking_headers = False
        peer.waiting.clear()
        peer.in_flight.clear()
        peer.last_progress = time.monotonic()
        self.log(f"catching up from {peer.label} (our height {self.service.chain.tip_height})")
        if peer.http_url and peer.chunks > self.service.store.sealed_chunks():
            self._spawn(self._sync_chunks(peer))  # whole days first, then the rest headers first
        else:
            self._request_headers(peer)

    def _end_sync(self, peer: Peer, *, useless: bool = False) -> None:
        tip = self.service.chain.tip
        if not useless:
            self.log(f"caught up with {peer.label} at height {tip.height}")
        peer.work = 0 if useless else min(peer.work, tip.chain_work)
        peer.header_tip, peer.more_headers = None, False
        peer.waiting.clear()
        peer.in_flight.clear()
        self.sync_peer = None
        self._maybe_sync()

    def _request_headers(self, peer: Peer) -> None:
        locator = self.service.chain.locator()
        if peer.header_tip is not None:  # continue after the headers we've checked already
            locator = [peer.header_tip.header.block_id] + locator[:MAX_LOCATOR - 1]
        peer.awaiting_headers = True
        peer.send({"type": "get_headers", "locator": [i.hex() for i in locator]})

    async def _sync_chunks(self, peer: Peer) -> None:
        """Download and import the peer's sealed chunks we don't have yet."""
        loop = asyncio.get_running_loop()
        params = self.service.params
        try:
            while peer is self.sync_peer and not peer.closed:
                index = self.service.store.sealed_chunks()
                if index >= peer.chunks:
                    break
                peer.last_progress = time.monotonic()
                try:
                    data = await download_chunk(peer.http_url, index, max_chunk_file_bytes(params))
                except (httpx.HTTPError, httpx.InvalidURL, ProtocolError) as error:
                    self.log(f"couldn't download chunk {index} from {peer.label} ({error}); going block by block")
                    break
                if data is None:
                    break
                try:
                    checked = await loop.run_in_executor(None, check_chunk_file, data, params)
                    self.service.import_chunk(checked, origin=peer)
                except ValidationError as error:
                    if error.code == "chunk-not-next":
                        break  # our chain differs from theirs here: go block by block
                    self._drop(peer, f"sent an invalid chunk {index} ({error.code})", ban=True)
                    return
                except ChunkError as error:
                    self._drop(peer, f"sent a damaged chunk {index} ({error})", ban=True)
                    return
                peer.last_progress = time.monotonic()
        finally:
            if peer is self.sync_peer and not peer.closed:
                self._request_headers(peer)

    def _on_headers(self, peer: Peer, message: dict) -> None:
        values = message.get("headers", [])
        if not isinstance(values, list) or len(values) > MAX_HEADERS:
            raise ProtocolError("bad headers list")
        if peer is not self.sync_peer or not peer.awaiting_headers:
            return  # we didn't ask (checking headers is expensive, so only one answer per question)
        peer.awaiting_headers = False
        try:
            headers = [header_from_bytes(bytes.fromhex(v)) for v in values]
        except (TypeError, ValueError, DecodeError):
            raise ProtocolError("unreadable header") from None
        for earlier, later in zip(headers, headers[1:]):
            if later.prev_id != earlier.block_id:
                raise ProtocolError("headers don't form a chain")
        peer.checking_headers = True
        self._spawn(self._check_headers(peer, headers, peer.sync_round))

    async def _check_headers(self, peer: Peer, headers: list, sync_round: int) -> None:
        """Check a batch of headers completely (proof of work on all cores, in the background)
        before any of their blocks gets downloaded."""
        chain, store = self.service.chain, self.service.store
        try:
            known = 0  # headers we have already form a prefix (a stored block's parent is stored too)
            for header in headers:
                stored = store.header(header.block_id)
                if stored is None:
                    break
                if stored.status == STATUS_INVALID:
                    raise ProtocolError("sent headers of an invalid block")
                known += 1
            new = headers[known:]
            if known:
                anchor = chain.header_tip(headers[known - 1].block_id)
            elif headers and peer.header_tip is not None and headers[0].prev_id == peer.header_tip.header.block_id:
                anchor = peer.header_tip
            elif headers:
                anchor = chain.header_tip(headers[0].prev_id)
            else:
                anchor = peer.header_tip
            if headers and anchor is None:
                # They don't connect to anything we know (the peer's chain changed under us?):
                # start over from our own chain. Not progress, so a peer doing it forever stalls out.
                if peer.sync_round == sync_round and peer is self.sync_peer:
                    peer.header_tip = None
                    peer.waiting.clear()
                    self._request_headers(peer)
                return
            checked = []
            if new:
                if anchor is not peer.header_tip:
                    chain.check_branch_point(anchor.header.block_id)
                anchor, checked = chain.check_headers(anchor, new)
                if checked:
                    loop = asyncio.get_running_loop()
                    if not await loop.run_in_executor(None, all_meet_target, checked, self.service.params.pow):
                        raise ProtocolError("sent headers with invalid proof of work")
                    chain.note_pow_checked(h.block_id for h in checked)
        except ValidationError as error:
            if error.code == "fork-below-finality":
                self.log(f"{peer.label} is on a branch that split off below our final history; not following it")
                if peer is self.sync_peer:
                    self._end_sync(peer, useless=True)
            else:
                self._drop(peer, f"sent invalid headers ({error.code})", ban=True)
            return
        except ProtocolError as error:
            self._drop(peer, str(error), ban=error.ban)
            return
        except Exception as error:  # a bug on our side
            self.service.log("".join(traceback.format_exception(error)))
            self._drop(peer, f"internal error while checking its headers ({error!r})", ban=False)
            return
        finally:
            if peer.sync_round == sync_round:  # a newer catch-up with this peer has its own check
                peer.checking_headers = False
        if peer is not self.sync_peer or peer.closed or peer.sync_round != sync_round:
            return
        peer.header_tip = anchor
        peer.waiting.extend(h.block_id for h in checked)
        peer.more_headers = len(headers) == MAX_HEADERS and len(checked) == len(new)
        if len(checked) < len(new):
            peer.work = 0  # the rest is too far ahead of our clock; don't come straight back for it
        if checked or known:
            peer.last_progress = time.monotonic()
        self._continue_sync(peer)

    def _continue_sync(self, peer: Peer) -> None:
        if peer is not self.sync_peer or peer.in_flight or peer.awaiting_headers or peer.checking_headers:
            return
        store = self.service.store
        while peer.waiting and store.header(peer.waiting[0]) is not None:
            peer.waiting.popleft()  # arrived meanwhile (e.g. announced by another peer)
        ours = self.service.chain.tip.chain_work
        better = peer.header_tip is not None and peer.header_tip.chain_work > ours
        if peer.waiting and better:
            batch = [peer.waiting.popleft() for _ in range(min(BLOCKS_PER_REQUEST, len(peer.waiting)))]
            peer.in_flight.update(batch)
            peer.send({"type": "get_data", "blocks": [b.hex() for b in batch], "txs": []})
        elif peer.more_headers and len(peer.waiting) < MAX_PENDING_HEADERS:
            self._request_headers(peer)
        else:
            if peer.waiting:
                self.log(f"{peer.label}'s chain has no more work than ours; not downloading it")
            self._end_sync(peer)

    def _check_stall(self) -> None:
        peer = self.sync_peer
        if peer is not None and time.monotonic() - peer.last_progress > STALL_TIMEOUT:
            self._drop(peer, "stopped sending what we asked for; trying another peer", ban=False)

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
