"""Request bodies for usage metering."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from .. import engines

_WHITESPACE = re.compile(r"\s+")


class UsageRequest(BaseModel):
    """Report consumption of a metered service.

    ``quantity`` is in the service's native unit:
      * minutes    for stt_* / voice_agent_web / voip_call
      * characters for tts_*
      * tokens     for translation / chat_agent
    """

    # camelCase accepted alongside snake_case, as on every other client-facing
    # body. `protected_namespaces` is cleared so the `model` field (which names
    # an AI model, not a Pydantic one) does not collide with `model_*`.
    #
    # `extra="forbid"` for the same reason signup uses it: a field this body
    # does not model is dropped silently otherwise, and the caller gets a 201
    # saying the whole thing was recorded. On a metering call that is the worst
    # available outcome — `engine_quantity` misspelt as `engineQty` would bill
    # the customer correctly while the allowance quietly under-reports, which
    # is precisely the failure the engine fields exist to prevent. A 422 names
    # the offending key instead.
    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        protected_namespaces=(),
        extra="forbid",
    )

    service: str = Field(description="One of the rate-card service keys.")
    quantity: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Total amount consumed, in the service unit. Omit when sending "
            "input_quantity and output_quantity."
        ),
    )
    # For services priced separately for input and output. Both or neither —
    # a request carrying only one side would have to be priced by guessing the
    # other, and guessing is the one thing a billing call must never do.
    input_quantity: float | None = Field(
        default=None, ge=0, description="Input/prompt units consumed."
    )
    output_quantity: float | None = Field(
        default=None, ge=0, description="Output/generated units consumed."
    )
    # Free text rather than an enum: models are added and renamed far more
    # often than this service is redeployed, so the caller owns the list and
    # only the shape is validated here.
    model: str | None = Field(
        default=None,
        max_length=64,
        description="ChatBucket model that served the request, e.g. 'CB Paluku'.",
    )
    # --- Engine capacity (our cost, never the customer's) ------------------
    # Which ChatBucket engine served the call and how much of *its* meter it
    # burned. Recorded for the operator view only: it is not priced, not
    # charged, and not echoed back to customers.
    engine: str | None = Field(
        default=None,
        max_length=64,
        description="ChatBucket engine that served this call, e.g. 'cb_vinu'.",
    )
    engine_quantity: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Amount consumed on the ENGINE's meter — not ours. Required "
            "whenever `engine` is set."
        ),
    )
    # Which upstream actually served this call, for reconciling our own bills.
    #
    # Free text, and deliberately *not* a list in this repo: the value comes
    # from each service's deployment config, so the identity of a supplier
    # lives where the credentials for it already live and never appears in
    # source, in a comment, or in this API's documentation.
    #
    # Operator-only, like the engine figures — projected out of everything a
    # customer can read.
    provider: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Upstream that served this call, for operator reconciliation. "
            "Set from the calling service's own configuration."
        ),
    )
    metadata: dict | None = Field(
        default=None, description="Optional caller context (session id, etc.)."
    )

    @field_validator("engine")
    @classmethod
    def _check_engine(cls, value: str | None) -> str | None:
        """Reject an unknown engine, unlike `model` which accepts anything.

        The rules differ because the callers do. `model` names are supplied by
        customers, who may send anything, and failing a billing call over an
        unrecognised one would be indefensible. `engine` is set by our own
        services against a fixed list of capabilities, so a typo is a bug in
        our code — and one that silently under-reports how much capacity we
        have burned, which is precisely the figure this field exists to keep
        honest. Better a 422 in development than a quota that looks healthier
        than it is.
        """
        if value is None:
            return None
        cleaned = _WHITESPACE.sub(" ", value.strip())
        if not cleaned:
            return None
        if not engines.is_known(cleaned):
            known = ", ".join(sorted(engines.ENGINES))
            raise ValueError(f"Unknown engine {cleaned!r}. Known engines: {known}.")
        return engines.normalize_engine_key(cleaned)

    @model_validator(mode="after")
    def _engine_needs_quantity(self) -> UsageRequest:
        """`engine` and `engine_quantity` travel together.

        The engine's amount is **not** defaulted from `quantity`, even where
        the units look identical. An engine may meter the audio it processed —
        including leading silence — while we bill the customer for the audio
        they sent, or count the characters it synthesised with markup included
        where we count only the text. Inferring one from the other would
        quietly misstate the burn in whichever direction the two happen to
        diverge. The caller knows what the engine actually consumed; it reports
        that, or reports nothing.
        """
        if self.engine and self.engine_quantity is None:
            raise ValueError(
                "engine_quantity is required when engine is set: the engine's "
                "meter is not derivable from ours."
            )
        if self.engine_quantity is not None and not self.engine:
            raise ValueError("engine_quantity was sent without an engine.")
        # A provider only means something as "the upstream behind this engine".
        # On its own it would be an unattributable row in the ops view.
        if self.provider and not self.engine:
            raise ValueError("provider was sent without an engine.")
        return self

    @field_validator("provider")
    @classmethod
    def _clean_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _WHITESPACE.sub(" ", value.strip())
        # Whitespace-only is a misconfigured env var, not a provider. Treated
        # as absent rather than becoming its own row in the reconciliation.
        return cleaned or None

    @model_validator(mode="after")
    def _one_pricing_form(self) -> UsageRequest:
        """Exactly one of: `quantity`, or both split quantities."""
        split_given = (self.input_quantity is not None, self.output_quantity is not None)
        if any(split_given) and not all(split_given):
            raise ValueError(
                "input_quantity and output_quantity must be sent together."
            )
        if all(split_given):
            if self.quantity is not None:
                raise ValueError(
                    "Send either quantity, or input_quantity + output_quantity "
                    "— not both. The total is derived from the split."
                )
            if self.input_quantity + self.output_quantity <= 0:
                raise ValueError("input_quantity + output_quantity must be > 0.")
        elif self.quantity is None:
            raise ValueError(
                "Send quantity, or input_quantity + output_quantity."
            )
        return self

    @property
    def total_quantity(self) -> float:
        """What was consumed in total, however it was reported."""
        if self.quantity is not None:
            return self.quantity
        return (self.input_quantity or 0) + (self.output_quantity or 0)

    @field_validator("model")
    @classmethod
    def _check_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _WHITESPACE.sub(" ", value.strip())
        # An all-whitespace model is a caller bug; treat it as absent rather
        # than storing "" as its own row in the breakdown.
        return cleaned or None
