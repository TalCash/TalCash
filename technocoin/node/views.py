"""JSON shapes the API returns.

Amounts are strings with all 6 decimals ("12.500000") so no client ever rounds
them; ids are lowercase hex; addresses are in text form.
"""

from ..core.amounts import format_amount
from ..core.block import Block, BlockHeader
from ..core.difficulty import difficulty
from ..core.params import NetworkParams
from ..core.tx import Coinbase, Transaction
from ..crypto.address import encode_address


def amount(units: int) -> str:
    return format_amount(units, fixed=True)


def memo_text(memo: bytes) -> str:
    return memo.decode("utf-8", errors="replace")


def tx_view(tx: Transaction, params: NetworkParams) -> dict:
    prefix = params.address_prefix
    if isinstance(tx, Coinbase):
        return {
            "txid": tx.txid.hex(),
            "type": "coinbase",
            "height": tx.height,
            "to": encode_address(tx.address, prefix),
            "amount": amount(tx.amount),
            "memo": memo_text(tx.memo),
            "size": tx.size,
        }
    return {
        "txid": tx.txid.hex(),
        "type": "transfer",
        "from": encode_address(tx.sender, prefix),
        "nonce": tx.nonce,
        "fee": amount(tx.fee),
        "outputs": [{"address": encode_address(o.address, prefix), "amount": amount(o.amount)} for o in tx.outputs],
        "memo": memo_text(tx.memo),
        "size": tx.size,
        "public_key": tx.sender_public_key.hex(),
        "signature": tx.signature.hex(),
    }


def header_view(header: BlockHeader, params: NetworkParams) -> dict:
    return {
        "id": header.block_id.hex(),
        "height": header.height,
        "prev_id": header.prev_id.hex(),
        "merkle_root": header.merkle_root.hex(),
        "snapshot_root": header.snapshot_root.hex(),
        "time": header.timestamp,
        "target": f"{header.target:064x}",
        "difficulty": difficulty(header.target, params),
        "nonce": header.nonce,
    }


def block_contents_view(block: Block, params: NetworkParams) -> dict:
    return {
        **header_view(block.header, params),
        "size": block.size,
        "transactions": [tx_view(tx, params) for tx in block.transactions],
    }


def block_view(block: Block, params: NetworkParams, *, on_main_chain: bool, confirmations: int) -> dict:
    return {**block_contents_view(block, params), "on_main_chain": on_main_chain, "confirmations": confirmations}
