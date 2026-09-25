"""Amounts are always integers counted in base units. Never use floats for money.

1 TC = 10**DECIMALS base units.
"""

import re

DECIMALS = 6
COIN = 10**DECIMALS

# Largest amount or balance the protocol allows (fits a signed 64-bit integer,
# so every database and language can store it exactly).
MAX_AMOUNT = 2**63 - 1

_AMOUNT_RE = re.compile(rf"^([0-9]+)(?:\.([0-9]{{1,{DECIMALS}}}))?$")  # ASCII digits only


def parse_amount(text: str) -> int:
    """Parse a human amount like "12.5" into base units (12_500_000)."""
    match = _AMOUNT_RE.match(text.strip())
    if not match:
        raise ValueError(f"invalid amount {text!r} (use up to {DECIMALS} decimals, no sign)")
    whole, frac = match.group(1), match.group(2) or ""
    units = int(whole) * COIN + int(frac.ljust(DECIMALS, "0"))
    if units > MAX_AMOUNT:
        raise ValueError(f"amount {text!r} is too large")
    return units


def format_amount(units: int, *, fixed: bool = False) -> str:
    """Format base units as a human amount. `fixed=True` always shows all decimals."""
    sign = "-" if units < 0 else ""
    whole, frac = divmod(abs(units), COIN)
    frac_text = f"{frac:0{DECIMALS}d}"
    if not fixed:
        frac_text = frac_text.rstrip("0")
    return f"{sign}{whole}.{frac_text}" if frac_text else f"{sign}{whole}"
