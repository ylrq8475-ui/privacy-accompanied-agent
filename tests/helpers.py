"""Synthetic fixtures only; no real user or model data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.app.schemas.actions import (
    ACActionPayload,
    ACMode,
    ActionAuthorization,
    ActionProposal,
    ActionType,
    AuthorizationStatus,
    MusicActionPayload,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=30)


def event_data() -> dict:
    return {
        "event_id": "event-001",
        "session_id": "session-001",
        "timestamp": NOW,
        "source_agent": "test-agent",
        "event_type": "CONTRACT_TEST",
        "payload": {"synthetic": True},
        "confidence": 0.8,
        "privacy_level": "INTERNAL",
        "network_scope": "LOCAL",
        "action_id": None,
        "latency_ms": 2,
        "status": "SUCCEEDED",
    }


def step3_data() -> dict:
    return {
        "state_hypotheses": [
            {
                "label": "PHYSICAL_FATIGUE",
                "confidence": 0.6,
                "evidence": ["synthetic statement"],
            },
            {
                "label": "EMOTIONAL_LOW",
                "confidence": 0.3,
                "evidence": [],
            },
        ],
        "recommended_action": {"type": "SUGGEST_MUSIC", "category": "calm_piano"},
        "recommendation_reason": [
            {"code": "synthetic-context", "text": "Synthetic test reason"}
        ],
        "clarification_candidates": [
            {
                "question_id": "question-001",
                "question": "Is this physical tiredness?",
                "target_labels": ["PHYSICAL_FATIGUE", "EMOTIONAL_LOW"],
            }
        ],
    }


def music_proposal() -> ActionProposal:
    return ActionProposal(
        action_id="music-001",
        session_id="session-001",
        action_type=ActionType.PLAY_MUSIC,
        payload=MusicActionPayload(track_id="calm_piano_01"),
        proposed_at=NOW,
        expires_at=LATER,
    )


def ac_proposal() -> ActionProposal:
    return ActionProposal(
        action_id="ac-001",
        session_id="session-001",
        action_type=ActionType.SET_AC,
        payload=ACActionPayload(
            device_id="living_room_ac",
            mode=ACMode.HEAT,
            target_temperature=24,
            duration_minutes=30,
        ),
        proposed_at=NOW,
        expires_at=LATER,
    )


def pending_authorization(action_id: str = "music-001") -> ActionAuthorization:
    return ActionAuthorization(
        action_id=action_id,
        action_type=ActionType.PLAY_MUSIC,
        authorization_status=AuthorizationStatus.PENDING,
        decided_at=None,
        expires_at=LATER,
    )


def approved_authorization(action_id: str = "music-001") -> ActionAuthorization:
    return ActionAuthorization(
        action_id=action_id,
        action_type=ActionType.PLAY_MUSIC,
        authorization_status=AuthorizationStatus.APPROVED,
        decided_at=NOW,
        expires_at=LATER,
    )


def connector_request(destination: str = "WEATHER_API", payload: dict | None = None) -> dict:
    return {
        "request_id": "request-001",
        "source_agent": "weather-agent",
        "destination": destination,
        "network_scope": "INTERNET",
        "payload": payload if payload is not None else {"city_code": "310000"},
        "timeout_ms": 5000,
        "max_response_bytes": 1_048_576,
        "max_attempts": 1,
        "created_at": NOW,
    }
