"""Request bodies for usage metering."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

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
    model_config = ConfigDict(
        populate_by_name=True, alias_generator=to_camel, protected_namespaces=()
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
        description="Model that served the request, e.g. 'Bulbul v3'.",
    )
    metadata: dict | None = Field(
        default=None, description="Optional caller context (session id, etc.)."
    )

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
