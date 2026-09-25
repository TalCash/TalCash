"""Transactions.

There are two kinds:
  * Transfer: one sender (identified by public key) pays one or more receivers.
    The sender signs every field, and `nonce` must equal the sender's count of
    previous transfers, so each transfer can be used exactly once.
  * Coinbase: the first transaction of every block, paying the block reward plus
    fees to the miner. It carries the block height so every coinbase is unique.

Byte layout (all integers big-endian):

  Transfer:  version u8 | network_id u8 | type u8 (=1) | sender_public_key 32
             | nonce u64 | fee u64 | output_count u8
             | output_count x (address 21 | amount u64)
             | memo_length u8 | memo | signature 64
  Coinbase:  version u8 | network_id u8 | type u8 (=0) | height u64
             | address 21 | amount u64 | memo_length u8 | memo

txid = SHA256(all bytes, including the signature).
The signature is Ed25519 over SIGNATURE_DOMAIN || (all bytes except the signature).
"""

from dataclasses import dataclass, replace
from functools import cached_property

from ..crypto import keys
from ..crypto.address import PAYLOAD_SIZE, is_known_payload, payload_from_public_key
from .amounts import MAX_AMOUNT
from .encoding import Reader, Writer
from .errors import DecodeError, ValidationError
from .hashing import sha256
from .params import NetworkParams

TX_VERSION = 1
TYPE_COINBASE = 0
TYPE_TRANSFER = 1

MAX_OUTPUTS = 255
MAX_MEMO_SIZE = 255
SIGNATURE_DOMAIN = b"TalCash/tx/1\x00"


@dataclass(frozen=True)
class Output:
    address: bytes  # 21-byte address payload
    amount: int


@dataclass(frozen=True)
class Transfer:
    network_id: int
    sender_public_key: bytes
    nonce: int
    fee: int
    outputs: tuple[Output, ...]
    memo: bytes = b""
    signature: bytes = bytes(keys.SIGNATURE_SIZE)

    def _write_unsigned(self, w: Writer) -> None:
        w.u8(TX_VERSION).u8(self.network_id).u8(TYPE_TRANSFER)
        w.fixed(self.sender_public_key, keys.PUBLIC_KEY_SIZE).u64(self.nonce).u64(self.fee)
        w.u8(len(self.outputs))
        for output in self.outputs:
            w.fixed(output.address, PAYLOAD_SIZE).u64(output.amount)
        w.var8(self.memo)

    def unsigned_bytes(self) -> bytes:
        w = Writer()
        self._write_unsigned(w)
        return w.getvalue()

    def serialize(self) -> bytes:
        w = Writer()
        self._write_unsigned(w)
        w.fixed(self.signature, keys.SIGNATURE_SIZE)
        return w.getvalue()

    @cached_property
    def txid(self) -> bytes:
        return sha256(self.serialize())

    @property
    def size(self) -> int:
        return len(self.serialize())

    @cached_property
    def sender(self) -> bytes:
        """Address payload of the sender."""
        return payload_from_public_key(self.sender_public_key)

    @property
    def total_spent(self) -> int:
        return sum(output.amount for output in self.outputs) + self.fee

    def signing_message(self) -> bytes:
        return SIGNATURE_DOMAIN + self.unsigned_bytes()

    def sign(self, private_key: bytes) -> "Transfer":
        if keys.public_key(private_key) != self.sender_public_key:
            raise ValueError("private key does not belong to the sender")
        return replace(self, signature=keys.sign(private_key, self.signing_message()))

    def has_valid_signature(self) -> bool:
        return keys.verify(self.sender_public_key, self.signing_message(), self.signature)


@dataclass(frozen=True)
class Coinbase:
    network_id: int
    height: int
    address: bytes  # 21-byte address payload of the miner
    amount: int
    memo: bytes = b""

    def serialize(self) -> bytes:
        w = Writer()
        w.u8(TX_VERSION).u8(self.network_id).u8(TYPE_COINBASE)
        w.u64(self.height).fixed(self.address, PAYLOAD_SIZE).u64(self.amount).var8(self.memo)
        return w.getvalue()

    @cached_property
    def txid(self) -> bytes:
        return sha256(self.serialize())

    @property
    def size(self) -> int:
        return len(self.serialize())


Transaction = Transfer | Coinbase


def read_transaction(r: Reader) -> Transaction:
    version = r.u8()
    if version != TX_VERSION:
        raise DecodeError(f"unknown transaction version {version}")
    network_id = r.u8()
    tx_type = r.u8()
    if tx_type == TYPE_TRANSFER:
        sender_public_key = r.fixed(keys.PUBLIC_KEY_SIZE)
        nonce = r.u64()
        fee = r.u64()
        outputs = tuple(Output(r.fixed(PAYLOAD_SIZE), r.u64()) for _ in range(r.u8()))
        memo = r.var8()
        signature = r.fixed(keys.SIGNATURE_SIZE)
        return Transfer(network_id, sender_public_key, nonce, fee, outputs, memo, signature)
    if tx_type == TYPE_COINBASE:
        height = r.u64()
        address = r.fixed(PAYLOAD_SIZE)
        amount = r.u64()
        memo = r.var8()
        return Coinbase(network_id, height, address, amount, memo)
    raise DecodeError(f"unknown transaction type {tx_type}")


def transaction_from_bytes(data: bytes) -> Transaction:
    r = Reader(data)
    tx = read_transaction(r)
    r.expect_end()
    return tx


def check_transaction(tx: Transaction, params: NetworkParams) -> None:
    """Rules that need no chain state. Raises ValidationError."""
    if tx.network_id != params.network_id:
        raise ValidationError("wrong-network", f"transaction is for network {tx.network_id}")
    if len(tx.memo) > MAX_MEMO_SIZE:
        raise ValidationError("memo-too-large")

    if isinstance(tx, Coinbase):
        if not is_known_payload(tx.address):
            raise ValidationError("bad-address", "coinbase pays an unknown address type")
        if not 0 <= tx.amount <= MAX_AMOUNT:
            raise ValidationError("bad-amount", "coinbase amount out of range")
        return

    if not 1 <= len(tx.outputs) <= MAX_OUTPUTS:
        raise ValidationError("bad-output-count", f"{len(tx.outputs)} outputs")
    for output in tx.outputs:
        if not is_known_payload(output.address):
            raise ValidationError("bad-address", "output pays an unknown address type")
        if not 1 <= output.amount <= MAX_AMOUNT:
            raise ValidationError("bad-amount", "every output must be at least 1 base unit")
    if not 0 <= tx.fee <= MAX_AMOUNT:
        raise ValidationError("bad-fee")
    if tx.total_spent > MAX_AMOUNT:
        raise ValidationError("amount-overflow")
    if not tx.has_valid_signature():
        raise ValidationError("bad-signature")
