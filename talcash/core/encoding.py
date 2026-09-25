"""Canonical binary encoding: fixed-width big-endian integers and length-prefixed bytes.

Every consensus object has exactly one valid encoding, so hashes and signatures
computed by different programs (node, miner, wallet) always agree.
"""

from .errors import DecodeError


def _uint_bytes(value: int, size: int) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"expected int, got {type(value).__name__}")
    if value < 0 or value >= 1 << (8 * size):
        raise ValueError(f"{value} does not fit in {size} unsigned bytes")
    return value.to_bytes(size, "big")


class Writer:
    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def u8(self, value: int) -> "Writer":
        self._parts.append(_uint_bytes(value, 1))
        return self

    def u32(self, value: int) -> "Writer":
        self._parts.append(_uint_bytes(value, 4))
        return self

    def u64(self, value: int) -> "Writer":
        self._parts.append(_uint_bytes(value, 8))
        return self

    def u256(self, value: int) -> "Writer":
        self._parts.append(_uint_bytes(value, 32))
        return self

    def fixed(self, data: bytes, size: int) -> "Writer":
        if len(data) != size:
            raise ValueError(f"expected {size} bytes, got {len(data)}")
        self._parts.append(bytes(data))
        return self

    def var8(self, data: bytes) -> "Writer":
        """Bytes with a one-byte length prefix (at most 255 bytes)."""
        self.u8(len(data))
        self._parts.append(bytes(data))
        return self

    def raw(self, data: bytes) -> "Writer":
        self._parts.append(bytes(data))
        return self

    def getvalue(self) -> bytes:
        return b"".join(self._parts)


class Reader:
    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)
        self._pos = 0

    def _take(self, size: int) -> bytes:
        end = self._pos + size
        if end > len(self._data):
            raise DecodeError(f"unexpected end of data (need {size} bytes at offset {self._pos})")
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def u32(self) -> int:
        return int.from_bytes(self._take(4), "big")

    def u64(self) -> int:
        return int.from_bytes(self._take(8), "big")

    def u256(self) -> int:
        return int.from_bytes(self._take(32), "big")

    def fixed(self, size: int) -> bytes:
        return self._take(size)

    def var8(self) -> bytes:
        return self._take(self.u8())

    @property
    def position(self) -> int:
        return self._pos

    def remaining(self) -> int:
        return len(self._data) - self._pos

    def expect_end(self) -> None:
        if self._pos != len(self._data):
            raise DecodeError(f"{len(self._data) - self._pos} trailing bytes")
