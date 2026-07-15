"""Service rate card and usage-cost calculation.

Single source of truth for what each metered service costs. Every price is in
INR and billed purely on usage:

    cost = rate * (quantity / unit_size)

* ``minutes``    services bill per minute            (unit_size = 1)
* ``characters`` services bill per 1000 characters   (unit_size = 1000)
* ``tokens``     services bill per 10000 tokens       (unit_size = 10000)

Rates are ``Decimal``, not float: ₹0.52 has no exact binary representation, so
a float rate card would bake an error into every charge before it is even
stored. See ``money.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from . import money


@dataclass(frozen=True)
class Service:
    key: str
    label: str
    unit: str  # "minutes" | "characters" | "tokens"
    rate: Decimal  # INR per `unit_size` units
    unit_size: int  # how many units one `rate` covers


# The rate card. Keys are the stable identifiers clients send to /usage.
SERVICES: dict[str, Service] = {
    s.key: s
    for s in [
        Service("stt_streaming", "Speech-to-Text (streaming)", "minutes", Decimal("0.52"), 1),
        Service("stt_offline", "Speech-to-Text (offline/upload)", "minutes", Decimal("0.39"), 1),
        Service("tts_streaming", "Text-to-Speech (streaming)", "characters", Decimal("0.91"), 1000),
        Service("tts_offline", "Text-to-Speech (offline/upload)", "characters", Decimal("0.78"), 1000),
        Service("translation", "Translation", "tokens", Decimal("7.5"), 10000),
        Service("chat_agent", "Chat Agent", "tokens", Decimal("4.38"), 10000),
        Service("voice_agent_web", "Voice Agent (web call)", "minutes", Decimal("4.0"), 1),
        Service("voip_call", "Voice Agent (VoIP call)", "minutes", Decimal("5.0"), 1),
    ]
}

SERVICE_KEYS = tuple(SERVICES.keys())


class UnknownServiceError(ValueError):
    pass


def get_service(key: str) -> Service:
    service = SERVICES.get(key)
    if service is None:
        raise UnknownServiceError(
            f"Unknown service '{key}'. Valid: {', '.join(SERVICE_KEYS)}"
        )
    return service


def calculate_cost(service_key: str, quantity: float | Decimal) -> Decimal:
    """Return the INR cost for ``quantity`` units of a service.

    ``quantity`` is expressed in the service's ``unit`` (minutes / characters /
    tokens). The result is an exact ``Decimal`` rounded to 4 decimal places, to
    keep sub-paisa precision; use ``money.to_json`` at the response boundary.
    """
    amount = money.to_decimal(quantity)
    if amount < 0:
        raise ValueError("quantity must be non-negative")
    service = get_service(service_key)
    return money.quantize(service.rate * amount / service.unit_size)


def rate_card() -> list[dict]:
    """A JSON-serializable description of every service and its price."""
    return [
        {
            "service": s.key,
            "label": s.label,
            "unit": s.unit,
            "rate": money.to_json(s.rate),
            "per": s.unit_size,
            "pricing": f"₹{s.rate} per {s.unit_size} {s.unit}"
            if s.unit_size != 1
            else f"₹{s.rate} per {s.unit[:-1]}",
        }
        for s in SERVICES.values()
    ]
