"""Bitcoin-style base58 (no 0, O, I, l). Each leading zero byte becomes a '1'."""

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: i for i, char in enumerate(ALPHABET)}


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    digits = []
    while number:
        number, rem = divmod(number, 58)
        digits.append(ALPHABET[rem])
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading_zeros + "".join(reversed(digits))


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        if char not in _INDEX:
            raise ValueError(f"invalid base58 character {char!r}")
        number = number * 58 + _INDEX[char]
    leading_ones = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * leading_ones + body
