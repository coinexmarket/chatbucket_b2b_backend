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

import re
from dataclasses import dataclass
from decimal import Decimal

from . import money

_WHITESPACE = re.compile(r"\s+")


def normalize_model_key(model: str) -> str:
    """Grouping/lookup key for a model name.

    Lower-cased with runs of whitespace collapsed, so "Bulbul v3",
    "bulbul  v3" and " Bulbul V3 " are one model for both pricing and the
    usage breakdown. Deliberately conservative — it folds only case and
    spacing, so two genuinely different models can never be merged.
    """
    return _WHITESPACE.sub(" ", model.strip()).lower()


@dataclass(frozen=True)
class Service:
    key: str
    label: str
    unit: str  # "minutes" | "characters" | "tokens"
    rate: Decimal  # INR per `unit_size` units
    unit_size: int  # how many units one `rate` covers
    # Token services can price input and output separately — generating a token
    # costs far more than reading one, and every major LLM API charges
    # accordingly. Set BOTH to enable split pricing for a service; leave both
    # unset and the flat `rate` above applies to the combined total.
    input_rate: Decimal | None = None
    output_rate: Decimal | None = None
    # Smallest billable amount, in the service's own unit. Consumption is
    # rounded UP to a multiple of it before pricing, which is how every telco
    # and speech API bills audio. `SECOND` on a minutes service means a
    # 12.3-second call bills as 13 seconds rather than an exact fraction.
    # None leaves the quantity exact — the current behaviour.
    billing_increment: Decimal | None = None

    @property
    def splits_input_output(self) -> bool:
        return self.input_rate is not None and self.output_rate is not None


# One second, expressed in minutes — the increment for audio services.
SECOND = Decimal(1) / Decimal(60)


# The rate card. Keys are the stable identifiers clients send to /usage.
SERVICES: dict[str, Service] = {
    s.key: s
    for s in [
        # Audio services bill fractional minutes exactly. To bill per whole
        # second instead — what telcos and speech APIs do — add
        # `billing_increment=SECOND` to each of the four `minutes` services.
        Service("stt_streaming", "Speech-to-Text (streaming)", "minutes", Decimal("0.52"), 1),
        Service("stt_offline", "Speech-to-Text (offline/upload)", "minutes", Decimal("0.39"), 1),
        Service("tts_streaming", "Text-to-Speech (streaming)", "characters", Decimal("0.91"), 1000),
        Service("tts_offline", "Text-to-Speech (offline/upload)", "characters", Decimal("0.78"), 1000),
        # The two token services are the candidates for split pricing. Add
        # `input_rate=` and `output_rate=` here to switch them over, e.g.
        #     Service("chat_agent", "Chat Agent", "tokens", Decimal("4.38"), 10000,
        #             input_rate=Decimal("1.50"), output_rate=Decimal("7.50")),
        # Both must be set; until then the flat rate below applies unchanged.
        Service("translation", "Translation", "tokens", Decimal("7.5"), 10000),
        Service("chat_agent", "Chat Agent", "tokens", Decimal("4.38"), 10000),
        Service("voice_agent_web", "Voice Agent (web call)", "minutes", Decimal("4.0"), 1),
        Service("voip_call", "Voice Agent (VoIP call)", "minutes", Decimal("5.0"), 1),
    ]
}

SERVICE_KEYS = tuple(SERVICES.keys())


@dataclass(frozen=True)
class ModelRate:
    """A price that applies to one model within a service.

    Overrides the service's rate when that model served the request — a large
    chat model need not cost the same as a small one. `unit_size` is optional
    and falls back to the service's, so a model priced per 1000 tokens can sit
    alongside a service quoted per 10,000.
    """

    service: str
    model: str  # display spelling; matching is case/space-insensitive
    rate: Decimal
    unit_size: int | None = None
    # Optional split pricing for this model specifically. A model override
    # always wins over the service, so a flat-rate override on a split-rate
    # service makes that model flat — the more specific price is the one that
    # applies, in both directions.
    input_rate: Decimal | None = None
    output_rate: Decimal | None = None


# Per-model overrides, keyed by (service, normalised model).
#
# EMPTY ON PURPOSE: with no entry, every model bills at its service's rate,
# exactly as before. Add real prices here — this is the single source of truth
# for them, the same way SERVICES is for the base rates:
#
#     ModelRate("chat_agent", "Sarvam 30b", Decimal("9.00")),
#     ModelRate("tts_streaming", "Bulbul v3", Decimal("1.20")),
#     ModelRate("chat_agent", "Big Model", Decimal("2.50"), unit_size=1000),
#
# A model with no entry is not an error: callers send arbitrary model names,
# and an unknown one must fall back to the service rate rather than fail the
# billing call.
MODEL_RATES: dict[tuple[str, str], ModelRate] = {
    (m.service, normalize_model_key(m.model)): m
    for m in [
        # e.g. ModelRate("chat_agent", "Sarvam 30b", Decimal("9.00")),
    ]
}


class UnknownServiceError(ValueError):
    pass


def get_service(key: str) -> Service:
    service = SERVICES.get(key)
    if service is None:
        raise UnknownServiceError(
            f"Unknown service '{key}'. Valid: {', '.join(SERVICE_KEYS)}"
        )
    return service


def resolve_rate(
    service_key: str, model: str | None = None
) -> tuple[Decimal, int, ModelRate | None]:
    """The rate actually charged for a service, given the model that served it.

    Returns ``(rate, unit_size, override)``. ``override`` is None when the
    service's own rate applies — either no model was reported, or that model
    has no entry in `MODEL_RATES`.
    """
    service = get_service(service_key)
    if model:
        override = MODEL_RATES.get((service.key, normalize_model_key(model)))
        if override is not None:
            return override.rate, override.unit_size or service.unit_size, override
    return service.rate, service.unit_size, None


def billable_quantity(service_key: str, quantity: float | Decimal) -> Decimal:
    """Round consumption up to the service's smallest billable unit.

    Returned unchanged when the service has no increment configured. Rounding
    happens before pricing, so the customer is charged for whole units of
    whatever they were told the unit is.
    """
    amount = money.to_decimal(quantity)
    increment = get_service(service_key).billing_increment
    if increment is None or increment <= 0:
        return amount
    from decimal import ROUND_CEILING

    return (amount / increment).to_integral_value(rounding=ROUND_CEILING) * increment


def calculate_cost(
    service_key: str, quantity: float | Decimal, model: str | None = None
) -> Decimal:
    """Return the INR cost for ``quantity`` units of a service.

    ``quantity`` is expressed in the service's ``unit`` (minutes / characters /
    tokens). When ``model`` has a per-model price it is used instead of the
    service's. The result is an exact ``Decimal`` rounded to 4 decimal places,
    to keep sub-paisa precision; use ``money.to_json`` at the response boundary.
    """
    amount = money.to_decimal(quantity)
    if amount < 0:
        raise ValueError("quantity must be non-negative")
    amount = billable_quantity(service_key, amount)
    rate, unit_size, _ = resolve_rate(service_key, model)
    return money.quantize(rate * amount / unit_size)


class SplitPricingUnavailableError(ValueError):
    pass


def split_rates(
    service_key: str, model: str | None = None
) -> tuple[Decimal, Decimal, int] | None:
    """``(input_rate, output_rate, unit_size)`` if split pricing applies.

    None means this service bills a single rate on the combined total. A model
    override replaces the service's pricing *entirely* — a flat-rate override
    on a split-rate service makes that model flat, because the more specific
    price is the one that applies.
    """
    service = get_service(service_key)
    if model:
        override = MODEL_RATES.get((service.key, normalize_model_key(model)))
        if override is not None:
            if override.input_rate is not None and override.output_rate is not None:
                return (
                    override.input_rate,
                    override.output_rate,
                    override.unit_size or service.unit_size,
                )
            return None
    if service.splits_input_output:
        return service.input_rate, service.output_rate, service.unit_size
    return None


def calculate_split_cost(
    service_key: str,
    input_quantity: float | Decimal,
    output_quantity: float | Decimal,
    model: str | None = None,
) -> Decimal:
    """Cost when input and output are priced separately.

    Quantized **once** at the end rather than per term — rounding each side
    first and adding would drift by up to half a unit on every call.
    """
    rates = split_rates(service_key, model)
    if rates is None:
        raise SplitPricingUnavailableError(
            f"Service '{service_key}' is not priced separately for input and "
            "output. Send `quantity` instead of input_quantity/output_quantity."
        )
    input_rate, output_rate, unit_size = rates
    incoming = money.to_decimal(input_quantity)
    outgoing = money.to_decimal(output_quantity)
    if incoming < 0 or outgoing < 0:
        raise ValueError("quantities must be non-negative")
    incoming = billable_quantity(service_key, incoming)
    outgoing = billable_quantity(service_key, outgoing)
    return money.quantize(
        (input_rate * incoming + output_rate * outgoing) / unit_size
    )


def models_for(service_key: str) -> list[dict]:
    """Per-model prices configured for one service, for the rate card."""
    return [
        {
            "model": m.model,
            "rate": money.to_json(m.rate),
            "per": m.unit_size or SERVICES[service_key].unit_size,
        }
        for (svc, _), m in MODEL_RATES.items()
        if svc == service_key
    ]


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
            # Set when input and output are priced separately; `rate` above is
            # then the legacy flat rate and no longer what gets charged.
            "input_rate": money.to_json(s.input_rate) if s.input_rate is not None else None,
            "output_rate": money.to_json(s.output_rate) if s.output_rate is not None else None,
            # NOT `pricing` — that key already carries the human-readable
            # price string ("₹0.91 per 1000 characters") the rate card has
            # always returned, and reusing it silently replaced that string.
            "pricing_mode": "split" if s.splits_input_output else "flat",
            # Smallest billable amount; null means exact fractions are billed.
            "billing_increment": money.to_json(s.billing_increment)
            if s.billing_increment is not None
            else None,
            # Models priced differently from the service. Empty means every
            # model on this service bills at the service rate above.
            "models": models_for(s.key),
        }
        for s in SERVICES.values()
    ]
