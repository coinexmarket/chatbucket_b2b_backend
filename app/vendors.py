"""Upstream vendor quota accounting — what serving a request cost *us*.

Deliberately separate from `pricing.py`. That module answers "what does the
customer owe us"; this one answers "how much of our Deepgram / Murf allowance
did serving them burn". They are different numbers, in different units, moving
in opposite directions on the P&L, and conflating them would make a healthy
margin look like a cost or vice versa.

Two consequences follow from keeping them apart:

* A vendor figure is **never charged to anyone**. Nothing here touches credits,
  invoices or the rate card. It is recorded alongside the usage event and read
  back only by the ops endpoint.
* A vendor figure is **not shown to customers**. Which supplier serves a call,
  and how much of our free tier is left, is our commercial information — see
  `routers/vendors.py` for how that is enforced.

**Free-tier sizes ship unset.** A quota is a fact about a contract this code
has no way to observe, and inventing one would produce a "credit remaining"
figure that reads as authoritative while being fiction — the same reason
`MODEL_RATES` ships empty and invoices refuse to compute tax. Set them with
`VENDOR_FREE_QUOTAS` and `remaining` starts being reported; leave them unset
and consumption is still counted, with `remaining: null` stating plainly that
nobody has told this service how big the allowance is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Vendor:
    key: str
    label: str
    # The unit the *vendor* meters in, which need not match the unit we bill
    # the customer in. Murf counts the characters it synthesises, including
    # markup our own character count excludes; Deepgram counts audio minutes it
    # processed, including the silence a customer would not expect to pay for.
    unit: str
    note: str


VENDORS: dict[str, Vendor] = {
    v.key: v
    for v in [
        Vendor(
            key="deepgram",
            label="Deepgram",
            unit="minutes",
            note="Speech-to-text (nova-3), billed per minute of audio.",
        ),
        Vendor(
            key="murf",
            label="Murf",
            unit="characters",
            note="Text-to-speech synthesis, billed per character.",
        ),
    ]
}


def normalize_vendor_key(value: str) -> str:
    """Lower-case and collapse whitespace, as model keys are normalised."""
    return _WHITESPACE.sub(" ", value.strip()).lower()


def get_vendor(key: str | None) -> Vendor | None:
    if not key:
        return None
    return VENDORS.get(normalize_vendor_key(key))


def is_known(key: str | None) -> bool:
    return get_vendor(key) is not None


def free_quota(key: str, quotas: dict[str, float]) -> float | None:
    """The configured free allowance for a vendor, or None if unset."""
    return quotas.get(normalize_vendor_key(key))


def summarise(
    key: str,
    consumed: float,
    events: int,
    quotas: dict[str, float],
) -> dict:
    """One vendor's line in the ops view.

    `remaining` and `percent_used` are **null when no quota is configured**
    rather than 0 or 100. Either number would be a claim about an allowance
    this service has not been told the size of, and "0 remaining" in particular
    would read as an outage that is not happening.
    """
    vendor = get_vendor(key)
    quota = free_quota(key, quotas)
    remaining = None if quota is None else max(0.0, quota - consumed)
    percent = None if not quota else round(min(100.0, consumed / quota * 100), 2)
    return {
        "vendor": key,
        "label": vendor.label if vendor else key,
        "unit": vendor.unit if vendor else None,
        "consumed": round(consumed, 4),
        "events": events,
        "free_quota": quota,
        "remaining": None if remaining is None else round(remaining, 4),
        "percent_used": percent,
        # True only when a quota is known *and* has been reached, so a caller
        # cannot mistake "we never configured this" for "we ran out".
        "exhausted": bool(quota is not None and consumed >= quota),
    }
