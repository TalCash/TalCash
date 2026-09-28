"""Abandoned sync rounds release downloads and cannot apply late worker results."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest

from talcash.core.params import REGTEST
from talcash.node import p2p
from talcash.node.p2p import P2PConfig, Peer, PeerManager
from talcash.node.service import NodeService
from talcash.node.store import Store


@asynccontextmanager
async def managed():
    service = NodeService(REGTEST, Store(":memory:"), log=lambda _: None)
    manager = PeerManager(service, P2PConfig())
    try:
        yield manager
    finally:
        manager.sync_peer = None
        manager._cancel_chunk_sync()
        tasks = list(manager._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        service.close()


def peer(name):
    async def nothing(*args):
        pass
    async def disconnect():
        raise OSError("connection lost")
    result = Peer(nothing, disconnect, nothing, outbound=False, label=name, host="127.0.0.1")
    result.http_url = "http://" + name
    result.ready, result.work, result.chunks = True, 1000000, 1
    return result


@pytest.mark.parametrize("reason", ["stall", "disconnect", "end", "shutdown"])
def test_abandoned_download_is_cancelled(monkeypatch, reason):
    async def run():
        started, released = asyncio.Event(), asyncio.Event()
        async def download(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                released.set()
        monkeypatch.setattr(p2p, "download_chunk", download)
        async with managed() as manager:
            source = peer("first")
            manager.peers.add(source)
            manager._start_sync(source)
            task = manager._chunk_task
            await asyncio.wait_for(started.wait(), 1)
            if reason == "stall":
                source.last_progress = time.monotonic() - p2p.STALL_TIMEOUT - 1
                manager._check_stall()
            elif reason == "disconnect":
                await manager._serve(source)
            elif reason == "end":
                manager._end_sync(source, useless=True)
            else:
                runner = asyncio.create_task(manager.run())
                await asyncio.sleep(0)
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
            await asyncio.wait_for(released.wait(), 1)
            await asyncio.gather(task, return_exceptions=True)
            assert manager._chunk_task is None
            assert manager.sync_peer is None
            messages = []
            while not source.queue.empty():
                message = source.queue.get_nowait()
                if isinstance(message, str):
                    messages.append(json.loads(message))
            assert not any(message["type"] == "get_headers" for message in messages)
    asyncio.run(run())


@pytest.mark.parametrize("replacement", ["same-peer", "other-peer", "headers-only"])
def test_late_download_from_previous_round_is_ignored(monkeypatch, replacement):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def download(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:
                started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    await release.wait()  # simulate an operation that finishes despite cancellation
                    return b"stale chunk"
            return None
        def forbidden(*args, **kwargs):
            pytest.fail("stale chunk reached validation")
        monkeypatch.setattr(p2p, "download_chunk", download)
        monkeypatch.setattr(p2p, "check_chunk_file", forbidden)
        async with managed() as manager:
            first = peer("first")
            second = first if replacement == "same-peer" else peer("second")
            if replacement == "headers-only":
                second.http_url = None
            manager._start_sync(first)
            old_task = manager._chunk_task
            await asyncio.wait_for(started.wait(), 1)
            manager._start_sync(second)
            new_task = manager._chunk_task
            release.set()
            await asyncio.wait_for(old_task, 1)
            if new_task is not None:
                await asyncio.wait_for(new_task, 1)
            assert manager.sync_peer is second
            assert second.awaiting_headers
            messages = [json.loads(second.queue.get_nowait()) for _ in range(second.queue.qsize())]
            assert [message["type"] for message in messages] == ["get_headers"]
    asyncio.run(run())


@pytest.mark.parametrize("obsolete_error", [False, True])
def test_cancelled_validation_is_drained_before_replacement_and_never_imported(monkeypatch, obsolete_error):
    async def run():
        started = asyncio.Queue()
        downloaded = []
        async def download(base, *args, **kwargs):
            downloaded.append(base)
            return base.encode()
        monkeypatch.setattr(p2p, "download_chunk", download)
        loop = asyncio.get_running_loop()
        workers = []
        def executor(*args):
            future = loop.create_future()
            workers.append(future)
            started.put_nowait(future)
            return future
        monkeypatch.setattr(loop, "run_in_executor", executor)
        async with managed() as manager:
            first, second = peer("first"), peer("second")
            imports = []
            def apply(checked, *, origin):
                imports.append((checked, origin))
                origin.chunks = 0
            monkeypatch.setattr(manager.service, "import_chunk", apply)
            manager._start_sync(first)
            old_task = manager._chunk_task
            first_worker = await asyncio.wait_for(started.get(), 1)
            manager._start_sync(second)
            new_task = manager._chunk_task
            await asyncio.gather(old_task, return_exceptions=True)
            assert not first_worker.cancelled()  # the executor thread cannot actually be stopped
            assert downloaded == ["http://first"]
            assert len(workers) == 1
            second.last_progress = time.monotonic() - p2p.STALL_TIMEOUT - 1
            manager._check_stall()
            assert manager.sync_peer is second  # waiting for local work isn't a network stall
            if obsolete_error:
                first_worker.set_exception(p2p.ChunkError("obsolete failure"))
            else:
                first_worker.set_result("obsolete")
            next_worker = await asyncio.wait_for(started.get(), 1)
            assert imports == []
            next_worker.set_result("current")
            await asyncio.wait_for(new_task, 1)
            assert imports == [("current", second)]
            assert manager._chunk_check is None
            assert manager._chunk_task is None
    asyncio.run(run())


def test_worker_completion_refreshes_deadline_before_sync_task_resumes():
    async def run():
        async with managed() as manager:
            source = peer("first")
            manager.sync_peer = source
            source.last_progress = time.monotonic() - p2p.STALL_TIMEOUT - 1
            async def wait():
                await asyncio.Future()
            manager._chunk_task = manager._spawn(wait())
            future = asyncio.get_running_loop().create_future()
            manager._chunk_check = future
            future.set_result(None)
            # The worker callback and stall timer can run before the sync task resumes.
            manager._chunk_check_finished(future)
            manager._check_stall()
            assert manager.sync_peer is source
            assert not source.closed
    asyncio.run(run())
