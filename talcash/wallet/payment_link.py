"""Payment links: `talcash:ADDRESS?amount=12.5&memo=order%2017&label=Coffee%20shop`.

One format for web links, QR codes and NFC tags, so any TalCash wallet can pay any request.
The rules are in docs/PROTOCOL.md (section 17); docs/payment-links.json has test cases that every
implementation should pass. Parsing is strict on purpose: a link one wallet accepts but reads
differently from another could make someone pay the wrong amount, so anything unusual is refused.
"""

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, unquote

from ..core.amounts import format_amount, parse_amount
from ..core.params import NETWORKS, NetworkParams
from ..core.tx import MAX_MEMO_SIZE
from ..crypto.address import decode_address

SCHEME = "talcash"
MAX_LINK_LENGTH = 2048  # characters; fits a QR code and an NFC tag
MAX_LABEL_LENGTH = 100  # characters

_NAME = re.compile(r"[a-z0-9-]+")
_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")  # a % not followed by two hex digits
_AMOUNT = re.compile(r"[0-9]+(\.[0-9]+)?")
# Characters that change how text around them is displayed (right-to-left overrides and the like):
# they could make "100" look like "001".
_BIDI = set("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")


class PaymentLinkError(ValueError):
    pass


@dataclass(frozen=True)
class PaymentRequest:
    network: str
    address: str
    amount: int | None = None  # base units; None: the payer chooses
    memo: str | None = None  # stored with the payment on the chain (public)
    label: str | None = None  # a name for the receiver, shown to the payer; not checked by anyone

    def to_link(self) -> str:
        parts = []
        if self.amount is not None:
            parts.append(f"amount={format_amount(self.amount)}")
        if self.memo is not None:
            parts.append(f"memo={quote(self.memo, safe='')}")
        if self.label is not None:
            parts.append(f"label={quote(self.label, safe='')}")
        link = f"{SCHEME}:{self.address}" + ("?" + "&".join(parts) if parts else "")
        if parse_link(link) != self:
            raise PaymentLinkError("this payment request can't be written as a valid link")
        return link


_ENDS = " \t\r\n"  # ignored at either end of a link (e.g. a line break after a scanned QR code)


def is_link(text: str) -> bool:
    return text.strip(_ENDS)[: len(SCHEME) + 1].lower() == SCHEME + ":"


def parse_link(text: str, params: NetworkParams | None = None) -> PaymentRequest:
    """Read a payment link, or raise PaymentLinkError saying what's wrong with it.
    With `params`, the link must be for that network."""
    text = text.strip(_ENDS)
    if len(text) > MAX_LINK_LENGTH:
        raise PaymentLinkError(f"the payment link is longer than {MAX_LINK_LENGTH} characters")
    if not is_link(text):
        raise PaymentLinkError(f"a payment link starts with {SCHEME}:")
    if "#" in text:  # browsers cut links at "#"; a memo like "order #17" is written order%20%2317
        raise PaymentLinkError("a payment link can't contain # (write it as %23)")
    address, question_mark, query = text[len(SCHEME) + 1:].partition("?")
    if question_mark and not query:
        raise PaymentLinkError("the payment link ends with an empty ?")
    network = _network_of(address)
    if params is not None and network.name != params.name:
        raise PaymentLinkError(f"this payment link is for {network.name}, not {params.name}")

    values: dict[str, str] = {}
    for part in query.split("&") if query else []:
        name, equals, raw = part.partition("=")
        if not equals or not _NAME.fullmatch(name):
            raise PaymentLinkError(f"malformed part {part[:40]!r} in the payment link")
        if name in values:
            raise PaymentLinkError(f"{name} appears twice in the payment link")
        values[name] = _decode(name, raw)

    for name in values:
        if name.startswith("req-"):
            raise PaymentLinkError(f"this payment link needs a wallet that understands {name!r}")
    amount = values.get("amount")
    if amount is not None:
        if not _AMOUNT.fullmatch(amount):
            raise PaymentLinkError(f"invalid amount {amount[:40]!r} in the payment link")
        try:
            amount = parse_amount(amount)
        except ValueError as error:
            raise PaymentLinkError(str(error)) from None
        if amount == 0:
            raise PaymentLinkError("the payment link asks for 0 TC")
    memo, label = values.get("memo"), values.get("label")
    if memo is not None and len(memo.encode("utf-8")) > MAX_MEMO_SIZE:
        raise PaymentLinkError(f"the memo in the payment link is longer than {MAX_MEMO_SIZE} bytes")
    if label is not None and len(label) > MAX_LABEL_LENGTH:
        raise PaymentLinkError(f"the label in the payment link is longer than {MAX_LABEL_LENGTH} characters")
    return PaymentRequest(network.name, address, amount, memo, label)


def _network_of(address: str) -> NetworkParams:
    for network in NETWORKS.values():
        if address.startswith(network.address_prefix):
            try:
                decode_address(address, network.address_prefix)
            except ValueError as error:
                raise PaymentLinkError(f"the address in the payment link is invalid: {error}") from None
            return network
    raise PaymentLinkError("the payment link has no TalCash address")


def _decode(name: str, raw: str) -> str:
    if not raw:
        raise PaymentLinkError(f"{name} is empty in the payment link")
    if _PERCENT.search(raw):
        raise PaymentLinkError(f"broken %-escape in {name} in the payment link")
    try:
        value = unquote(raw, errors="strict")  # "+" stays "+": spaces are written %20
    except UnicodeDecodeError:
        raise PaymentLinkError(f"{name} in the payment link isn't valid UTF-8") from None
    for character in value:
        if unicodedata.category(character) in ("Cc", "Zl", "Zp") or character in _BIDI:
            raise PaymentLinkError(f"{name} in the payment link contains a control character")
    return value
