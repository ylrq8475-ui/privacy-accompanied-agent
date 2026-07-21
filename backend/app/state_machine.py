"""Explicit Phase 1B state machine for the fixed pure-Mock scenario."""

from __future__ import annotations

from enum import StrEnum


class DemoState(StrEnum):
    IDLE = "IDLE"
    PERSON_DETECTED = "PERSON_DETECTED"
    CONTEXT_READY = "CONTEXT_READY"
    LISTENING = "LISTENING"
    MOOD_ANALYSIS = "MOOD_ANALYSIS"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    USER_CLARIFIED = "USER_CLARIFIED"
    MEMORY_RETRIEVED = "MEMORY_RETRIEVED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    WAITING_MUSIC_AUTHORIZATION = "WAITING_MUSIC_AUTHORIZATION"
    MUSIC_AUTHORIZED = "MUSIC_AUTHORIZED"
    MUSIC_EXECUTED = "MUSIC_EXECUTED"
    WAITING_AC_AUTHORIZATION = "WAITING_AC_AUTHORIZATION"
    AC_AUTHORIZED = "AC_AUTHORIZED"
    AC_EXECUTED = "AC_EXECUTED"
    COMPLETED = "COMPLETED"

    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    API_FAILED = "API_FAILED"
    CAMERA_FAILED = "CAMERA_FAILED"
    ASR_FAILED = "ASR_FAILED"
    PRIVACY_REJECTED = "PRIVACY_REJECTED"
    ACTION_REJECTED = "ACTION_REJECTED"
    ACTION_EXPIRED = "ACTION_EXPIRED"
    ACTION_FAILED = "ACTION_FAILED"


ALLOWED_TRANSITIONS: dict[DemoState, frozenset[DemoState]] = {
    DemoState.IDLE: frozenset(
        {DemoState.PERSON_DETECTED, DemoState.MOOD_ANALYSIS, DemoState.CAMERA_FAILED}
    ),
    DemoState.PERSON_DETECTED: frozenset(
        {DemoState.CONTEXT_READY, DemoState.API_FAILED, DemoState.PRIVACY_REJECTED}
    ),
    DemoState.CONTEXT_READY: frozenset({DemoState.LISTENING}),
    DemoState.LISTENING: frozenset({DemoState.MOOD_ANALYSIS, DemoState.ASR_FAILED}),
    DemoState.MOOD_ANALYSIS: frozenset(
        {
            DemoState.CLARIFICATION_REQUIRED,
            DemoState.MEMORY_RETRIEVED,
            DemoState.MODEL_TIMEOUT,
        }
    ),
    DemoState.CLARIFICATION_REQUIRED: frozenset({DemoState.USER_CLARIFIED}),
    DemoState.USER_CLARIFIED: frozenset({DemoState.MEMORY_RETRIEVED}),
    DemoState.MEMORY_RETRIEVED: frozenset(
        {DemoState.ACTION_PROPOSED, DemoState.COMPLETED}
    ),
    DemoState.ACTION_PROPOSED: frozenset(
        {
            DemoState.WAITING_MUSIC_AUTHORIZATION,
            DemoState.WAITING_AC_AUTHORIZATION,
            DemoState.COMPLETED,
        }
    ),
    DemoState.WAITING_MUSIC_AUTHORIZATION: frozenset(
        {DemoState.MUSIC_AUTHORIZED, DemoState.ACTION_REJECTED, DemoState.ACTION_EXPIRED}
    ),
    DemoState.MUSIC_AUTHORIZED: frozenset(
        {DemoState.MUSIC_EXECUTED, DemoState.ACTION_FAILED}
    ),
    DemoState.MUSIC_EXECUTED: frozenset(
        {DemoState.WAITING_AC_AUTHORIZATION, DemoState.COMPLETED}
    ),
    DemoState.WAITING_AC_AUTHORIZATION: frozenset(
        {DemoState.AC_AUTHORIZED, DemoState.ACTION_REJECTED, DemoState.ACTION_EXPIRED}
    ),
    DemoState.AC_AUTHORIZED: frozenset({DemoState.AC_EXECUTED, DemoState.ACTION_FAILED}),
    DemoState.AC_EXECUTED: frozenset({DemoState.COMPLETED}),
    DemoState.ACTION_REJECTED: frozenset(
        {DemoState.WAITING_AC_AUTHORIZATION, DemoState.COMPLETED}
    ),
    DemoState.ACTION_EXPIRED: frozenset(
        {DemoState.WAITING_AC_AUTHORIZATION, DemoState.COMPLETED}
    ),
    DemoState.ACTION_FAILED: frozenset(
        {DemoState.WAITING_AC_AUTHORIZATION, DemoState.COMPLETED}
    ),
    DemoState.COMPLETED: frozenset(),
    DemoState.MODEL_TIMEOUT: frozenset(),
    DemoState.API_FAILED: frozenset(),
    DemoState.CAMERA_FAILED: frozenset(),
    DemoState.ASR_FAILED: frozenset(),
    DemoState.PRIVACY_REJECTED: frozenset(),
}


class InvalidStateTransition(ValueError):
    """Raised when a caller attempts to bypass the fixed state graph."""


def ensure_state_transition(current: DemoState, target: DemoState) -> DemoState:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransition(f"cannot transition from {current.value} to {target.value}")
    return target
