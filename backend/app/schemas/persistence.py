"""Strict Phase 1C contracts for the minimal Demo persistence boundary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, field_validator, model_validator

from .actions import (
    ACActionPayload,
    ActionType,
    ActionTypeValue,
    AuthorizationStatus,
    AuthorizationStatusValue,
    ExecutionStatus,
    ExecutionStatusValue,
    MusicActionPayload,
)
from .base import Identifier, StrictModel, require_aware_datetime
from .events import NetworkScopeValue
from .analysis import TextStateLabel, TextStateLabelValue
from .reaction import ReactionTone, ReactionToneValue


PayloadDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class MemoryConfirmationRequest(StrictModel):
    context: Literal["workday_evening"]
    preference: Literal["calm_piano"]
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_boolean_true(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("confirmed must be the boolean true")
        return value


class MemoryRecord(MemoryConfirmationRequest):
    memory_id: Identifier
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return require_aware_datetime(value, "memory timestamp")


class PersistedAction(StrictModel):
    action_id: Identifier
    session_id: Identifier
    action_type: ActionTypeValue
    authorization_status: AuthorizationStatusValue
    execution_status: ExecutionStatusValue
    payload: MusicActionPayload | ACActionPayload
    created_at: datetime
    expires_at: datetime
    authorized_at: datetime | None
    executed_at: datetime | None
    result: dict[str, JsonValue] | None

    @model_validator(mode="after")
    def validate_action(self) -> "PersistedAction":
        for field_name in ("created_at", "expires_at"):
            require_aware_datetime(getattr(self, field_name), field_name)
        for field_name in ("authorized_at", "executed_at"):
            value = getattr(self, field_name)
            if value is not None:
                require_aware_datetime(value, field_name)
        expected_prefix = "music-" if self.action_type is ActionType.PLAY_MUSIC else "ac-"
        if not self.action_id.startswith(expected_prefix):
            raise ValueError(f"action_id must start with {expected_prefix}")
        if self.action_type is ActionType.PLAY_MUSIC and not isinstance(
            self.payload, MusicActionPayload
        ):
            raise ValueError("PLAY_MUSIC requires MusicActionPayload")
        if self.action_type is ActionType.SET_AC and not isinstance(
            self.payload, ACActionPayload
        ):
            raise ValueError("SET_AC requires ACActionPayload")
        if self.authorization_status is AuthorizationStatus.PENDING:
            if self.authorized_at is not None:
                raise ValueError("PENDING action cannot have authorized_at")
        elif self.authorized_at is None:
            raise ValueError("decided authorization requires authorized_at")
        if self.execution_status in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            if self.executed_at is None or self.result is None:
                raise ValueError("terminal action requires executed_at and result")
        return self


class PersistedAuditRecord(StrictModel):
    audit_id: Identifier
    session_id: Identifier
    event_type: Identifier
    network_scope: NetworkScopeValue
    privacy_result: Identifier
    payload_digest: PayloadDigest
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value, "created_at")


class ReplyStyle(StrEnum):
    GENTLE = "GENTLE"
    CONCISE = "CONCISE"
    DIRECT = "DIRECT"


class MusicPreference(StrEnum):
    EMOTION_MATCHED = "EMOTION_MATCHED"
    NONE = "NONE"


ReplyStyleValue = Annotated[ReplyStyle, Field(strict=False)]
MusicPreferenceValue = Annotated[MusicPreference, Field(strict=False)]
PreferenceKey = Literal["reply_style", "music_preference"]


class UserPreferenceConfirmation(StrictModel):
    key: PreferenceKey
    value: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_key_value_pair(self) -> "UserPreferenceConfirmation":
        allowed = (
            {item.value for item in ReplyStyle}
            if self.key == "reply_style"
            else {item.value for item in MusicPreference}
        )
        if self.value not in allowed:
            raise ValueError(f"invalid value for {self.key}")
        return self


class UserPreferenceRecord(StrictModel):
    key: PreferenceKey
    value: str = Field(min_length=1, max_length=32)
    confirmed: Literal[True] = True
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_record(self) -> "UserPreferenceRecord":
        UserPreferenceConfirmation(key=self.key, value=self.value)
        require_aware_datetime(self.created_at, "created_at")
        require_aware_datetime(self.updated_at, "updated_at")
        return self


class UserPreferenceView(StrictModel):
    value: str | None
    confirmed: bool
    runtime_default: bool


class UserPreferencesResponse(StrictModel):
    reply_style: UserPreferenceView
    music_preference: UserPreferenceView


class ConfidenceBand(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ActionOutcome(StrEnum):
    NOT_SUGGESTED = "NOT_SUGGESTED"
    POLICY_REJECTED = "POLICY_REJECTED"
    USER_REJECTED = "USER_REJECTED"
    EXPIRED = "EXPIRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CLOSED_UNDECIDED = "CLOSED_UNDECIDED"


ConfidenceBandValue = Annotated[ConfidenceBand, Field(strict=False)]
ActionOutcomeValue = Annotated[ActionOutcome, Field(strict=False)]


class EmotionSummary(StrictModel):
    summary_id: Identifier
    session_id: Identifier
    final_emotion: TextStateLabelValue
    user_confirmed: bool
    confidence_band: ConfidenceBandValue
    reaction_tone: ReactionToneValue
    music_outcome: ActionOutcomeValue
    ac_outcome: ActionOutcomeValue
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def validate_summary_timestamp(cls, value: datetime) -> datetime:
        return require_aware_datetime(value, "created_at")


class EmotionHistoryContext(StrictModel):
    state_counts: dict[TextStateLabelValue, int]
    recent_states: list[TextStateLabelValue] = Field(max_length=5)


class StateConfirmationRequest(StrictModel):
    label: TextStateLabelValue
