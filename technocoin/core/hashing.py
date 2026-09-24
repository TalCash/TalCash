import hashlib
from collections.abc import Sequence


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    """Merkle tree root as defined in RFC 6962 (Certificate Transparency).

    Leaf hash = SHA256(0x00 || leaf), inner hash = SHA256(0x01 || left || right).
    A tree of n leaves splits at the largest power of two below n. The 0x00/0x01
    prefixes and the absence of node duplication make it impossible to build two
    different leaf lists with the same root (a known weakness of Bitcoin's tree).
    """
    if not leaves:
        raise ValueError("merkle tree needs at least one leaf")
    # Built bottom-up: hash neighbours in pairs and carry an odd last node up
    # unchanged. This gives exactly the RFC 6962 root, without recursion.
    level = [hashlib.sha256(b"\x00" + leaf).digest() for leaf in leaves]
    while len(level) > 1:
        paired = [hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest() for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            paired.append(level[-1])
        level = paired
    return level[0]
