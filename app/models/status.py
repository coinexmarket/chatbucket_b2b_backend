"""Request bodies for service status reporting."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from ..status import STATUSES


class _StatusBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )

    status: str = Field(description=f"One of: {', '.join(STATUSES)}")
    detail: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in STATUSES:
            raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
        return cleaned


class HeartbeatRequest(_StatusBase):
    service: str = Field(description="System key, e.g. 'tts'.")
    # Defaulted so the common case is a bare "I'm alive" ping; a service that
    # knows it is struggling can say `degraded` instead.
    status: str = Field(default="operational")


class StatusUpdateRequest(_StatusBase):
    """Manual override. `status` is required — setting one is a decision."""
