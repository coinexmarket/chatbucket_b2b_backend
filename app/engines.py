"""Engine capacity accounting — what serving a request cost *us*.

Deliberately separate from `pricing.py`. That module answers "what does the
customer owe us"; this one answers "how much of our own engine allowance did
serving them burn". They are different numbers, in different units, moving in
opposite directions on the P&L, and conflating them would make a healthy margin
look like a cost or vice versa.

Two consequences follow from keeping them apart:

* An engine figure is **never charged to anyone**. Nothing here touches
  credits, invoices or the rate card. It is recorded alongside the usage event
  and read back only by the operator endpoint.
* An engine figure is **not shown to customers**. How much of an allowance is
  left is our commercial information — see `routers/engines.py` for how that is
  enforced.

Engines are named for the ChatBucket capability they serve — `cb_vinu`,
`cb_palukulu` — matching the model names the dashboard already shows. Whatever
sits behind an engine is an implementation detail that is **never named here**,
in an identifier, a comment, a log line or an error message: such a name spreads
into stored records and API responses, where anyone reading a stack trace or an
env var learns how the product is built. A capability can also be re-provisioned
without renaming a field that history is keyed on.

**Free allowances ship unset.** Capacity is a fact about an arrangement this
code has no way to observe, and inventing one would produce a "remaining"
figure that reads as authoritative while being fiction — the same reason
`MODEL_RATES` ships empty and invoices refuse to compute tax. Set them with
`ENGINE_FREE_QUOTAS` and `remaining` starts being reported; leave them unset and
consumption is still counted, with `remaining: null` stating plainly that nobody
has told this service how big the allowance is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Engine:
    key: str
    label: str
    capability: str
    # The unit the *engine* meters in, which need not match the unit we bill
    # the customer in: an engine may count the audio it processed including
    # leading silence, or the characters it synthesised including markup, where
    # we bill only what the customer sent.
    unit: str


ENGINES: dict[str, Engine] = {
    e.key: e
    for e in [
        Engine("cb_vinu", "CB Vinu", "Speech to Text", "minutes"),
        Engine("cb_palukulu", "CB Palukulu", "Text to Speech", "characters"),
        Engine("cb_vaaradhi", "CB Vaaradhi", "Translation", "tokens"),
        Engine("cb_thodu", "CB Thodu", "Chat + Voice Agent", "tokens"),
    ]
}


def normalize_engine_key(value: str) -> str:
    """Lower-case and collapse whitespace, as model keys are normalised."""
    return _WHITESPACE.sub(" ", value.strip()).lower()


def get_engine(key: str | None) -> Engine | None:
    if not key:
        return None
    return ENGINES.get(normalize_engine_key(key))


def is_known(key: str | None) -> bool:
    return get_engine(key) is not None


def free_quota(key: str, quotas: dict[str, float]) -> float | None:
    """The configured allowance for an engine, or None if unset."""
    return quotas.get(normalize_engine_key(key))


def summarise(
    key: str,
    consumed: float,
    events: int,
    quotas: dict[str, float],
) -> dict:
    """One engine's line in the operator view.

    `remaining` and `percent_used` are **null when no quota is configured**
    rather than 0 or 100. Either number would be a claim about an allowance
    this service has not been told the size of, and "0 remaining" in particular
    would read as an outage that is not happening.
    """
    engine = get_engine(key)
    quota = free_quota(key, quotas)
    remaining = None if quota is None else max(0.0, quota - consumed)
    percent = None if not quota else round(min(100.0, consumed / quota * 100), 2)
    return {
        "engine": key,
        "label": engine.label if engine else key,
        "capability": engine.capability if engine else None,
        "unit": engine.unit if engine else None,
        "consumed": round(consumed, 4),
        "events": events,
        "free_quota": quota,
        "remaining": None if remaining is None else round(remaining, 4),
        "percent_used": percent,
        # True only when a quota is known *and* has been reached, so a caller
        # cannot mistake "we never configured this" for "we ran out".
        "exhausted": bool(quota is not None and consumed >= quota),
    }
