"""Unified event and shared boundary enums."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, JsonValue, field_validator

from .base import Identifier, ShortText, StrictModel, require_aware_datetime


class PrivacyLevel(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    SENSITIVE = "SENSITIVE"


class NetworkScope(StrEnum):
    LOCAL = "LOCAL"
    LAN = "LAN"
    INTERNET = "INTERNET"


PrivacyLevelValue = Annotated[PrivacyLevel, Field(strict=False)]
NetworkScopeValue = Annotated[NetworkScope, Field(strict=False)]


class Event(StrictModel):
    event_id: Identifier
    session_id: Identifier
    timestamp: datetime
    source_agent: Identifier
    event_type: Identifier
    payload: dict[str, JsonValue]
    confidence: float | None
    privacy_level: PrivacyLevelValue
    network_scope: NetworkScopeValue
    action_id: Identifier | None
    latency_ms: int | None = Field(ge=0)
    status: ShortText

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return require_aware_datetime(value, "timestamp")

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, value: float | None) -> float | None:
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return value
