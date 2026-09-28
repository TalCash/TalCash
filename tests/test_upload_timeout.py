"""Request-body deadlines cover both stalled and continuously trickling uploads."""

import asyncio
import json

import pytest

from talcash.node.limits import BodyLimitMiddleware


@pytest.mark.parametrize("trickle", [False, True])
def test_upload_deadline_rejects_stalled_and_trickling_clients(trickle):
    async def run():
        sent, received = [], []
        async def app(*args):
            pytest.fail("incomplete request reached application")
        async def receive():
            if trickle:
                await asyncio.sleep(0.005)
                received.append(1)
                return {"type": "http.request", "body": b"x", "more_body": True}
            await asyncio.Future()
        async def send(message):
            sent.append(message)
        middleware = BodyLimitMiddleware(app, 1000000, body_timeout=0.05)
        await asyncio.wait_for(middleware({"type": "http"}, receive, send), timeout=1)
        assert sent[0]["status"] == 408
        assert (b"connection", b"close") in sent[0]["headers"]
        assert json.loads(sent[1]["body"])["error"] == "request-timeout"
        if trickle:
            assert received
    asyncio.run(run())


def test_completed_upload_does_not_limit_application_execution():
    async def run():
        sent = []
        async def receive():
            return {"type": "http.request", "body": b"complete", "more_body": False}
        async def app(scope, receive, send):
            assert (await receive())["body"] == b"complete"
            await asyncio.sleep(0.03)  # only uploading, not processing, has a deadline
            await send({"type": "http.response.start", "status": 200, "headers": []})
        async def send(message):
            sent.append(message)
        await BodyLimitMiddleware(app, 100, body_timeout=0.01)({"type": "http"}, receive, send)
        assert [message["status"] for message in sent] == [200]
    asyncio.run(run())


def test_upload_disconnect_has_no_response():
    async def run():
        async def receive():
            return {"type": "http.disconnect"}
        async def forbidden(*args):
            pytest.fail("disconnected client reached application or got a response")
        await BodyLimitMiddleware(forbidden, 100, body_timeout=0.01)(
            {"type": "http"}, receive, forbidden
        )
    asyncio.run(run())


def test_external_cancellation_is_not_changed_into_timeout_response():
    async def run():
        started = asyncio.Event()
        async def receive():
            started.set()
            await asyncio.Future()
        async def forbidden(*args):
            pytest.fail("cancelled request reached application or got a response")
        middleware = BodyLimitMiddleware(forbidden, 100)
        task = asyncio.create_task(middleware({"type": "http"}, receive, forbidden))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_body_deadline_does_not_interrupt_sending_size_error():
    async def run():
        sent = []
        async def receive():
            return {"type": "http.request", "body": b"too large", "more_body": False}
        async def forbidden(*args):
            pytest.fail("oversized request reached application")
        async def send(message):
            sent.append(message)
            await asyncio.sleep(0.02)
        await BodyLimitMiddleware(forbidden, 1, body_timeout=0.01)({"type": "http"}, receive, send)
        assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [413]
    asyncio.run(run())
