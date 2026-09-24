import pytest

from technocoin.core.amounts import COIN, MAX_AMOUNT, format_amount, parse_amount
from technocoin.core.encoding import Reader, Writer
from technocoin.core.errors import DecodeError


def test_integers_round_trip_big_endian():
    data = Writer().u8(7).u32(0x01020304).u64(2**64 - 1).u256(1).getvalue()
    assert data[1:5] == b"\x01\x02\x03\x04"
    r = Reader(data)
    assert (r.u8(), r.u32(), r.u64(), r.u256()) == (7, 0x01020304, 2**64 - 1, 1)
    r.expect_end()


@pytest.mark.parametrize("value", [-1, 256])
def test_writer_rejects_out_of_range(value):
    with pytest.raises(ValueError):
        Writer().u8(value)


def test_writer_rejects_non_integers():
    with pytest.raises(TypeError):
        Writer().u64(1.5)
    with pytest.raises(TypeError):
        Writer().u8(True)


def test_var8_limits():
    assert Reader(Writer().var8(b"abc").getvalue()).var8() == b"abc"
    with pytest.raises(ValueError):
        Writer().var8(bytes(256))


def test_reader_errors():
    with pytest.raises(DecodeError):
        Reader(b"\x00\x01").u32()
    r = Reader(b"\x00\x01")
    r.u8()
    with pytest.raises(DecodeError):
        r.expect_end()


@pytest.mark.parametrize(
    "text, units",
    [("1", COIN), ("0.000001", 1), ("12.5", 12_500_000), ("10.000000", 10 * COIN), (" 3 ", 3 * COIN)],
)
def test_parse_amount(text, units):
    assert parse_amount(text) == units


@pytest.mark.parametrize("text", ["1.0000001", "-1", "1e5", "", ".5", "1.", "abc", "9" * 20])
def test_parse_amount_rejects(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_format_amount():
    assert format_amount(1) == "0.000001"
    assert format_amount(12_500_000) == "12.5"
    assert format_amount(10 * COIN) == "10"
    assert format_amount(12_500_000, fixed=True) == "12.500000"
    assert parse_amount(format_amount(MAX_AMOUNT)) == MAX_AMOUNT
