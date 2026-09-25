"""Running a node: the API server and, optionally, mining, in one process."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import uvicorn

from ..core.block import Block
from ..core.params import NetworkParams
from ..miner.engine import Miner
from ..paths import network_dir
from .api import ApiPolicy, create_app
from .chain import Outcome
from .limits import is_loopback
from .p2p import MAX_MESSAGE_BYTES, P2PConfig
from .service import NodeService, print_now

HOUSEKEEPING_SECONDS = 60


async def mining_loop(
    service: NodeService,
    miner: bytes,
    *,
    workers: int | None = None,
    blocks: int | None = None,
    on_finished: Callable[[], None] | None = None,
) -> None:
    """Mine on the node's chain. Switches to new work at once when the tip changes."""
    params = service.params
    loop = asyncio.get_running_loop()
    refresh = max(1, params.target_spacing // 6)  # rebuild the template this often (fresh time, new transfers)
    engine = await loop.run_in_executor(None, Miner, workers)  # starting processes takes a moment
    try:
        service.log(f"mining with {engine.workers} processes, paying {service.address_text(miner)}")
        found, last_hashes, last_time = 0, 0, time.monotonic()
        while blocks is None or found < blocks:
            template = service.template(miner)
            version = service.tip_version
            engine.mine(template.header, params.pow)
            deadline = time.monotonic() + refresh
            nonce = None
            while nonce is None and time.monotonic() < deadline and service.tip_version == version:
                nonce = await loop.run_in_executor(None, engine.wait, 0.2)
            if nonce is None:
                continue
            hashes, now = engine.total_hashes(), time.monotonic()
            rate = (hashes - last_hashes) / max(now - last_time, 1e-9)
            last_hashes, last_time = hashes, now
            block = Block(replace(template.header, nonce=nonce), template.transactions)
            result = service.submit_block(block, source="mined", note=f"{rate:.0f} H/s")
            if result.outcome is Outcome.NEW_TIP:
                found += 1
            elif result.outcome is Outcome.SIDE_BRANCH:
                service.log("found a block, but another block at that height arrived first")
            elif result.outcome is Outcome.INVALID:
                service.log(f"own block rejected: {result.error}")
            # DUPLICATE: another node found and sent the very same block first; nothing to report
    finally:
        await loop.run_in_executor(None, engine.close)
    if on_finished is not None:
        on_finished()


async def housekeeping(service: NodeService) -> None:
    while True:
        await asyncio.sleep(HOUSEKEEPING_SECONDS)
        expired = service.mempool.expire()
        if expired:
            service.log(f"dropped {expired} transfer(s) that waited too long")


def run_node(
    params: NetworkParams,
    base: Path | None = None,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    miner: bytes | None = None,
    workers: int | None = None,
    blocks: int | None = None,
    min_fee_per_byte: int = 1,
    peers: list[str] | None = None,
    public_url: str | None = None,
    trusted: list[str] | None = None,
    log: Callable[[str], None] = print_now,
) -> None:
    """Run until Ctrl+C (or until `blocks` blocks are mined).

    `peers` are nodes to stay connected to (ws://host:port/v1/p2p). `public_url` is how other
    nodes can reach this one; by default it's derived from host and port when those are specific.
    Listening anywhere but this computer (e.g. --host 0.0.0.0) puts the API in public mode (see
    api.py); `trusted` addresses still get full access.
    """
    port = params.default_port if port is None else port
    if public_url is None and host not in ("0.0.0.0", "::") and port != 0:
        public_url = f"ws://{host}:{port}/v1/p2p"
    p2p = P2PConfig(listen_url=public_url, connect=list(peers or []),
                    peers_file=network_dir(params.name, base) / "peers.json")
    public = not is_loopback(host)
    policy = ApiPolicy(public=public, trusted=frozenset(trusted or []))
    server: uvicorn.Server | None = None

    def stop() -> None:
        server.should_exit = True

    def open_service() -> NodeService:
        service = NodeService.open(params, base, min_fee_per_byte=min_fee_per_byte, log=log)
        log(service.describe())
        log(f"API on http://{host}:{port}/v1/status (interactive docs: http://{host}:{port}/docs)")
        if public_url:
            log(f"other nodes can connect to {public_url}")
        if public:
            log("public mode: other computers get a rate-limited API without mining"
                + (f" (full access: {', '.join(sorted(policy.trusted))})" if policy.trusted else ""))
        return service

    background = [housekeeping]
    if miner is not None:
        background.append(lambda service: mining_loop(
            service, miner, workers=workers, blocks=blocks, on_finished=stop if blocks is not None else None))

    config = uvicorn.Config(create_app(open_service, background, p2p, policy), host=host, port=port,
                            log_level="warning", ws="websockets-sansio", ws_max_size=MAX_MESSAGE_BYTES, lifespan="on",
                            limit_concurrency=2000 if public else None)
    server = uvicorn.Server(config)
    server.run()
