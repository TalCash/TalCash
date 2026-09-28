"""Resource limits reject hostile input before buffering or expensive decoding."""

import asyncio
import zlib

import pytest

from talcash.core.encoding import Writer
from talcash.core.hashing import sha256
from talcash.core.params import REGTEST
from talcash.node import blockfiles
from talcash.node.chain import check_chunk_file
from talcash.node.limits import BodyLimitMiddleware


def test_body_limit_stops_reading_at_limit():
    async def run():
        reads, sent = [], []

        async def app(scope, receive, send):
            pytest.fail("oversized input reached application")

        async def receive():
            reads.append(1)
            assert len(reads) <= 2
            return {"type": "http.request", "body": b"123456", "more_body": True}

        async def send(message):
            sent.append(message)

        await BodyLimitMiddleware(app, 10)({"type": "http", "headers": []}, receive, send)
        assert len(reads) == 2
        assert sent[0]["status"] == 413

    asyncio.run(run())


@pytest.mark.parametrize("length", [b"-1", b"x", b"9" * 5000])
def test_body_limit_rejects_bad_length_without_reading(length):
    async def run():
        sent = []

        async def forbidden(*args):
            pytest.fail("invalid Content-Length must be rejected before reading")

        async def send(message):
            sent.append(message)

        await BodyLimitMiddleware(forbidden, 10)(
            {"type": "http", "headers": [(b"content-length", length)]}, forbidden, send
        )
        assert sent[0]["status"] == 413

    asyncio.run(run())


def chunk_header(count, segments):
    return (Writer().raw(blockfiles.MAGIC).u8(REGTEST.network_id).u32(0).u64(0)
            .u32(count).fixed(bytes(32), 32).u32(segments))


def checked_bytes(writer):
    body = writer.getvalue()
    return body + sha256(body)


def test_chunk_repeated_segment_is_rejected_before_decompression(monkeypatch):
    packed = zlib.compress(b"anything")
    offset = blockfiles._HEADER_BYTES + 24
    data = checked_bytes(chunk_header(128, 2).u64(offset).u32(len(packed))
                         .u64(offset).u32(len(packed)).raw(packed))

    def forbidden(*args):
        pytest.fail("invalid table reached decompression")

    monkeypatch.setattr(blockfiles, "_unpack_segment", forbidden)
    with pytest.raises(blockfiles.ChunkError, match="layout"):
        blockfiles.decode_chunk(data)


def test_chunk_network_block_limit_is_checked_before_decompression(monkeypatch):
    count = REGTEST.chunk_size + 1
    segments = (count + 63) // 64
    data = checked_bytes(chunk_header(count, segments))

    def forbidden(*args):
        pytest.fail("oversized chunk reached decompression")

    monkeypatch.setattr(blockfiles, "_unpack_segment", forbidden)
    with pytest.raises(blockfiles.ChunkError, match="number of blocks"):
        check_chunk_file(data, REGTEST)


@pytest.mark.parametrize("raw", [
    b"\0" * 4,  # zero-length records can otherwise create millions of objects
    b"\0\0",  # truncated length
    b"\0\0\0\x05abc",  # truncated block
    b"\0\0\0\x01x" * 65,  # too many blocks in a single segment
])
def test_segment_rejects_invalid_records(raw):
    with pytest.raises(zlib.error):
        blockfiles._unpack_segment(zlib.compress(raw))
