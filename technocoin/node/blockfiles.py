"""Block files: the node's permanent record of the chain.

The database is only an index plus balances; everything in it can be rebuilt
from these files (`tc node --reindex`). Copy the `blocks` folder to another
machine and it has the whole chain.

    blocks/recent/<height>-<block id>.block   one block not yet sealed into a chunk (any branch)
    blocks/chunks/<index>.chunk               a sealed chunk: one day of final blocks

Block files hold the block's exact canonical bytes. Files are written once and
never changed; a recent file is deleted after its block is sealed into a chunk
(or, for a losing branch, once that height is final).

Chunk file layout (integers big-endian):
    magic "TCCHUNK1" | network_id u8 | chunk index u32 | first height u64 | block count u32
    | chunk root 32 (RFC 6962 merkle root of the block ids) | segment count u32
    | segment table: count x (file offset u64, length u32)
    | segments: each zlib-compressed, holding up to 64 blocks as (length u32, block bytes)
    | checksum 32 (SHA-256 of everything before it)
Blocks are compressed in groups of 64: almost as small as compressing the whole
file (about 40% of the raw size), yet reading one block only unpacks its group.
"""

import os
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from ..core.block import HEADER_SIZE, Block, header_from_bytes
from ..core.encoding import Reader, Writer
from ..core.errors import DecodeError
from ..core.hashing import merkle_root, sha256

MAGIC = b"TCCHUNK1"
SEGMENT_BLOCKS = 64
_HEADER_BYTES = 8 + 1 + 4 + 8 + 4 + 32 + 4
# 64 full blocks (1 MB each, the limit on every network) with their length fields. A segment that
# would unpack to more is refused before it's unpacked any further (a "zip bomb" can't fill memory).
MAX_SEGMENT_BYTES = SEGMENT_BLOCKS * (1_000_000 + 4)


class ChunkError(ValueError):
    """A chunk file is damaged, altered, or not a chunk file."""


@dataclass(frozen=True)
class ChunkLocation:
    """Where a block sits inside a chunk file."""

    chunk: int
    segment_offset: int
    segment_length: int
    position: int  # index within the segment


@dataclass(frozen=True)
class ChunkFile:
    network_id: int
    index: int
    first_height: int
    blocks: list[bytes]
    locations: list[ChunkLocation]
    root: bytes


def block_id_of(block_bytes: bytes) -> bytes:
    return sha256(block_bytes[:HEADER_SIZE])


def _pack_segment(blocks: list[bytes]) -> bytes:
    return zlib.compress(b"".join(len(b).to_bytes(4, "big") + b for b in blocks), 6)


def _unpack_segment(data: bytes) -> list[bytes]:
    unpacker = zlib.decompressobj()
    raw = unpacker.decompress(data, MAX_SEGMENT_BYTES)
    if unpacker.unconsumed_tail:
        raise zlib.error("segment unpacks to more than 64 full blocks")
    if not unpacker.eof or unpacker.unused_data:
        raise zlib.error("segment is cut short or has extra bytes")
    blocks, offset = [], 0
    while offset < len(raw):
        length = int.from_bytes(raw[offset:offset + 4], "big")
        blocks.append(raw[offset + 4:offset + 4 + length])
        offset += 4 + length
    return blocks


def encode_chunk(network_id: int, index: int, first_height: int, blocks: list[bytes]) -> tuple[bytes, list[ChunkLocation]]:
    groups = [blocks[i:i + SEGMENT_BLOCKS] for i in range(0, len(blocks), SEGMENT_BLOCKS)]
    segments = [_pack_segment(group) for group in groups]
    root = merkle_root([block_id_of(b) for b in blocks])
    offset = _HEADER_BYTES + 12 * len(segments)
    table, locations = Writer(), []
    for group, segment in zip(groups, segments):
        table.u64(offset).u32(len(segment))
        locations.extend(ChunkLocation(index, offset, len(segment), position) for position in range(len(group)))
        offset += len(segment)
    head = Writer().raw(MAGIC).u8(network_id).u32(index).u64(first_height).u32(len(blocks)).fixed(root, 32)
    body = head.u32(len(segments)).getvalue() + table.getvalue() + b"".join(segments)
    return body + sha256(body), locations


def decode_chunk(data: bytes) -> ChunkFile:
    """Read and fully verify a chunk file (checksum, structure, merkle root, block links)."""
    if len(data) < _HEADER_BYTES + 32 or not data.startswith(MAGIC):
        raise ChunkError("not a TechnoCoin chunk file")
    body, checksum = data[:-32], data[-32:]
    if sha256(body) != checksum:
        raise ChunkError("checksum mismatch: the file is damaged or was changed")
    try:
        r = Reader(body)
        r.fixed(8)
        network_id, index, first_height, count, root, segment_count = (
            r.u8(), r.u32(), r.u64(), r.u32(), r.fixed(32), r.u32())
        table = [(r.u64(), r.u32()) for _ in range(segment_count)]
        blocks, locations = [], []
        for offset, length in table:
            group = _unpack_segment(body[offset:offset + length])
            blocks.extend(group)
            locations.extend(ChunkLocation(index, offset, length, position) for position in range(len(group)))
    except (DecodeError, zlib.error) as error:
        raise ChunkError(f"unreadable chunk: {error}") from None
    if len(blocks) != count or not blocks:
        raise ChunkError("wrong number of blocks")
    if merkle_root([block_id_of(b) for b in blocks]) != root:
        raise ChunkError("chunk root mismatch")
    previous_id = None
    for height, block in enumerate(blocks, first_height):
        try:
            header = header_from_bytes(block[:HEADER_SIZE])
        except DecodeError:
            raise ChunkError(f"unreadable block header at height {height}") from None
        if header.height != height or (previous_id is not None and header.prev_id != previous_id):
            raise ChunkError(f"blocks don't form a chain at height {height}")
        previous_id = header.block_id
    return ChunkFile(network_id, index, first_height, blocks, locations, root)


RECENT_CACHE_BYTES = 64 * 1024 * 1024


class BlockFiles:
    """Reads and writes block files. With folder=None everything stays in memory (for tests).

    Recently written or read blocks are also kept in memory (up to 64 MB): opening a freshly
    written file can take several milliseconds (antivirus scanners), and the node reads recent
    blocks often (to apply them, to unlock rewards 100 blocks later, to serve peers).
    """

    def __init__(self, folder: Path | None) -> None:
        self.folder = Path(folder) if folder is not None else None
        self._memory: dict[str, bytes] = {}
        self._segments: OrderedDict[tuple[int, int], list[bytes]] = OrderedDict()
        self._recent_cache: OrderedDict[str, bytes] = OrderedDict()
        self._recent_cache_bytes = 0

    def _cache_put(self, name: str, data: bytes) -> None:
        if name in self._recent_cache:
            self._cache_drop(name)
        self._recent_cache[name] = data
        self._recent_cache_bytes += len(data)
        while self._recent_cache_bytes > RECENT_CACHE_BYTES:
            self._cache_drop(next(iter(self._recent_cache)))

    def _cache_drop(self, name: str) -> None:
        data = self._recent_cache.pop(name, None)
        if data is not None:
            self._recent_cache_bytes -= len(data)

    # --- a tiny file layer (disk or memory) ------------------------------------

    def _write(self, name: str, data: bytes) -> None:
        if self.folder is None:
            self._memory[name] = data
            return
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)  # never a half-written file under the real name

    def _read(self, name: str, offset: int = 0, length: int | None = None) -> bytes:
        if self.folder is None:
            data = self._memory[name]
            return data[offset:offset + length] if length is not None else data
        with open(self.folder / name, "rb") as file:
            file.seek(offset)
            return file.read() if length is None else file.read(length)

    def _exists(self, name: str) -> bool:
        return name in self._memory if self.folder is None else (self.folder / name).exists()

    def _delete(self, name: str) -> None:
        if self.folder is None:
            self._memory.pop(name, None)
        else:
            (self.folder / name).unlink(missing_ok=True)

    def _names(self, prefix: str) -> list[str]:
        if self.folder is None:
            return sorted(n for n in self._memory if n.startswith(prefix))
        directory = self.folder / prefix
        return sorted(f"{prefix}{p.name}" for p in directory.glob("*") if not p.name.endswith(".tmp")) \
            if directory.exists() else []

    # --- recent blocks -----------------------------------------------------------

    @staticmethod
    def recent_name(height: int, block_id: bytes) -> str:
        return f"recent/{height:010d}-{block_id.hex()}.block"

    def write_recent(self, block: Block) -> None:
        name, data = self.recent_name(block.height, block.block_id), block.serialize()
        self._write(name, data)
        self._cache_put(name, data)

    def read_recent(self, height: int, block_id: bytes) -> bytes:
        name = self.recent_name(height, block_id)
        data = self._recent_cache.get(name)
        if data is None:
            data = self._read(name)
            self._cache_put(name, data)
        else:
            self._recent_cache.move_to_end(name)
        return data

    def delete_recent(self, height: int, block_id: bytes) -> None:
        name = self.recent_name(height, block_id)
        self._cache_drop(name)
        self._delete(name)

    def recent_blocks(self) -> list[tuple[int, bytes]]:
        """(height, block id) of every recent block file, lowest height first."""
        found = []
        for name in self._names("recent/"):
            stem = name[len("recent/"):].removesuffix(".block")
            height, _, hex_id = stem.partition("-")
            if height.isdigit() and len(hex_id) == 64:
                found.append((int(height), bytes.fromhex(hex_id)))
        return sorted(found)

    # --- chunks --------------------------------------------------------------------

    @staticmethod
    def chunk_name(index: int) -> str:
        return f"chunks/{index:06d}.chunk"

    def write_chunk(self, network_id: int, index: int, first_height: int, blocks: list[bytes]) -> list[ChunkLocation]:
        data, locations = encode_chunk(network_id, index, first_height, blocks)
        self._write(self.chunk_name(index), data)
        return locations

    def save_chunk_bytes(self, index: int, data: bytes) -> None:
        """Store a chunk file received as-is (already verified with decode_chunk)."""
        self._write(self.chunk_name(index), data)

    def delete_chunk(self, index: int) -> None:
        self._delete(self.chunk_name(index))

    def has_chunk(self, index: int) -> bool:
        return self._exists(self.chunk_name(index))

    def chunk_bytes(self, index: int) -> bytes:
        return self._read(self.chunk_name(index))

    def chunk_path(self, index: int) -> Path | None:
        return self.folder / self.chunk_name(index) if self.folder is not None else None

    def chunk_indexes(self) -> list[int]:
        names = self._names("chunks/")
        return [int(n[len("chunks/"):].removesuffix(".chunk")) for n in names if n.endswith(".chunk")]

    def read_from_chunk(self, location: ChunkLocation) -> bytes:
        key = (location.chunk, location.segment_offset)
        blocks = self._segments.get(key)
        if blocks is None:
            blocks = _unpack_segment(self._read(self.chunk_name(location.chunk),
                                                location.segment_offset, location.segment_length))
            self._segments[key] = blocks
            if len(self._segments) > 64:
                self._segments.popitem(last=False)
        else:
            self._segments.move_to_end(key)
        return blocks[location.position]
