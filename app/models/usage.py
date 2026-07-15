"""Request bodies for usage metering."""
from __future__ import annotations

from pydantic import BaseModel, Field


class UsageRequest(BaseModel):
    """Report consumption of a metered service.

    ``quantity`` is in the service's native unit:
      * minutes    for stt_* / voice_agent_web / voip_call
      * characters for tts_*
      * tokens     for translation / chat_agent
    """

    service: str = Field(description="One of the rate-card service keys.")
    quantity: float = Field(gt=0, description="Amount consumed, in the service unit.")
    metadata: dict | None = Field(
        default=None, description="Optional caller context (session id, etc.)."
    )
