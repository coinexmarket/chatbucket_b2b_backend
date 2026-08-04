"""Money handling — exact decimal amounts, in one place.

Every amount here is INR carried to 4 decimal places (sub-paisa), which binary
floats cannot represent: `0.1 + 0.2` is `0.30000000000000004`, and that error
compounds once Mongo `$sum`s thousands of usage rows into an invoice total.

So money moves through three representations, and this module owns the edges:

* `Decimal`   — arithmetic (`pricing.calculate_cost`);
* `Decimal128` — storage, which Mongo's `$sum` adds exactly;
* `float`     — the JSON wire, and *only* there.

Nothing outside this module should call `float()` on an amount.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from bson.decimal128 import Decimal128

# Sub-paisa precision: 1 unit = ₹0.0001. The rate card is priced to 4dp, so
# this is what every stored cost is rounded to.
EXPONENT = Decimal("0.0001")


def quantize(amount: Decimal) -> Decimal:
    """Round to the currency's 4dp, half-up.

    Half-up is what an invoice reader expects. Python's own default is
    half-even, which would round ₹0.00005 down to ₹0.0000 and look like a bug.
    """
    return amount.quantize(EXPONENT, rounding=ROUND_HALF_UP)


def to_decimal(value: Decimal | Decimal128 | float | int | str) -> Decimal:
    """Coerce an amount from any layer into a `Decimal`.

    Floats convert via `str` on purpose: that yields the shortest decimal which
    reads back as the same float (`0.1` -> `"0.1"`), rather than the exact
    binary expansion (`0.1000000000000000055511151231257827…`). This is also
    the path that reads usage records written before costs were Decimal128.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def to_bson(amount: Decimal | float | int | str) -> Decimal128:
    """Render an amount for storage, quantized to the currency's precision."""
    return Decimal128(quantize(to_decimal(amount)))


def to_json(value: Decimal | Decimal128 | float | int | str) -> float:
    """Render an amount for the JSON wire.

    The one sanctioned float conversion. JSON has no decimal type and every
    client parses numbers as float64 regardless, so precision beyond this point
    is not ours to keep. Lossless in practice: float64 carries ~15 significant
    digits, and a 4dp amount only exhausts that past ₹10 billion.
    """
    return float(to_decimal(value))


PAISE = Decimal("0.01")


def to_paise(amount: Decimal | float | int | str) -> int:
    """Render an amount as whole paise, for a payment gateway.

    Gateways take integer minor units, so an amount that is not a whole number
    of paise cannot be charged exactly. Raises rather than rounding: charging
    ₹100.56 for a ₹100.5551 order would make the money taken differ from the
    money recorded, which is the one thing billing must never do. Quantize with
    `to_chargeable` first.
    """
    value = to_decimal(amount)
    paise = value * 100
    if paise != paise.to_integral_value():
        raise ValueError(
            f"{value} is not a whole number of paise and cannot be charged exactly."
        )
    return int(paise)


def to_chargeable(amount: Decimal | float | int | str) -> Decimal:
    """Round an amount to whole paise, half-up — what a gateway can charge."""
    return to_decimal(amount).quantize(PAISE, rounding=ROUND_HALF_UP)


def total(amounts) -> Decimal:
    """Sum amounts exactly. `sum()` would start from the int 0 and is fine,
    but this states the intent and keeps the quantize in one place."""
    return quantize(sum((to_decimal(a) for a in amounts), Decimal("0")))
