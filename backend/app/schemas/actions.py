"""Independent music and AC action contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, JsonValue, model_validator

from .base import Identifier, StrictModel, require_aware_datetime


class ActionType(StrEnum):
    PLAY_MUSIC = "PLAY_MUSIC"
    SET_AC = "SET_AC"


class AuthorizationStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class ExecutionStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ACMode(StrEnum):
    HEAT = "heat"
    COOL = "cool"
    AUTO = "auto"


ActionTypeValue = Annotated[ActionType, Field(strict=False)]
AuthorizationStatusValue = Annotated[AuthorizationStatus, Field(strict=False)]
ExecutionStatusValue = Annotated[ExecutionStatus, Field(strict=False)]
ACModeValue = Annotated[ACMode, Field(strict=False)]


class MusicActionPayload(StrictModel):
    track_id: Identifier


class ACActionPayload(StrictModel):
    device_id: Identifier
    mode: ACModeValue
    target_temperature: int = Field(ge=16, le=30)
    duration_minutes: int = Field(ge=1, le=240)


def _require_action_identity(action_id: str, action_type: ActionType) -> None:
    expected_prefix = "music-" if action_type is ActionType.PLAY_MUSIC else "ac-"
    if not action_id.startswith(expected_prefix):
        raise ValueError(f"{action_type.value} action_id must start with {expected_prefix}")


class ActionProposal(StrictModel):
    action_id: Identifier
    session_id: Identifier
    action_type: ActionTypeValue
    payload: MusicActionPayload | ACActionPayload
    proposed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_proposal(self) -> "ActionProposal":
        _require_action_identity(self.action_id, self.action_type)
        require_aware_datetime(self.proposed_at, "proposed_at")
        require_aware_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.proposed_at:
            raise ValueError("expires_at must be after proposed_at")
        if self.action_type is ActionType.PLAY_MUSIC and not isinstance(self.payload, MusicActionPayload):
            raise ValueError("PLAY_MUSIC requires MusicActionPayload")
        if self.action_type is ActionType.SET_AC and not isinstance(self.payload, ACActionPayload):
            raise ValueError("SET_AC requires ACActionPayload")
        return self


class ActionAuthorization(StrictModel):
    action_id: Identifier
    action_type: ActionTypeValue
    authorization_status: AuthorizationStatusValue
    decided_at: datetime | None
    expires_at: datetime

    @model_validator(mode="after")
    def validate_authorization(self) -> "ActionAuthorization":
        _require_action_identity(self.action_id, self.action_type)
        require_aware_datetime(self.expires_at, "expires_at")
        if self.authorization_status is AuthorizationStatus.PENDING:
            if self.decided_at is not None:
                raise ValueError("PENDING authorization cannot have decided_at")
            return self
        if self.decided_at is None:
            raise ValueError("non-PENDING authorization requires decided_at")
        require_aware_datetime(self.decided_at, "decided_at")
        if self.authorization_status is AuthorizationStatus.APPROVED and self.expires_at <= self.decided_at:
            raise ValueError("approved authorization must expire after decided_at")
        return self


class ActionExecution(StrictModel):
    action_id: Identifier
    action_type: ActionTypeValue
    execution_status: ExecutionStatusValue
    started_at: datetime | None
    finished_at: datetime | None

    @model_validator(mode="after")
    def validate_execution(self) -> "ActionExecution":
        _require_action_identity(self.action_id, self.action_type)
        if self.started_at is not None:
            require_aware_datetime(self.started_at, "started_at")
        if self.finished_at is not None:
            require_aware_datetime(self.finished_at, "finished_at")
        if self.execution_status is ExecutionStatus.NOT_STARTED:
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError("NOT_STARTED cannot have timestamps")
        elif self.execution_status is ExecutionStatus.RUNNING:
            if self.started_at is None or self.finished_at is not None:
                raise ValueError("RUNNING requires started_at and no finished_at")
        else:
            if self.started_at is None or self.finished_at is None:
                raise ValueError("terminal execution requires both timestamps")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at cannot precede started_at")
        return self


class ActionResult(StrictModel):
    action_id: Identifier
    action_type: ActionTypeValue
    execution_status: ExecutionStatusValue
    result: dict[str, JsonValue]
    completed_at: datetime

    @model_validator(mode="after")
    def validate_result(self) -> "ActionResult":
        _require_action_identity(self.action_id, self.action_type)
        require_aware_datetime(self.completed_at, "completed_at")
        if self.execution_status not in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            raise ValueError("ActionResult requires a terminal execution status")
        return self
