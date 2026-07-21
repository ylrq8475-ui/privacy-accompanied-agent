from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.app.schemas.actions import (
    ACActionPayload,
    ACMode,
    ActionProposal,
    ActionType,
    MusicActionPayload,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def proposals(
    *,
    session_id: str = "session-persistence-001",
    proposed_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> tuple[ActionProposal, ActionProposal]:
    expiry = expires_at or proposed_at + timedelta(minutes=5)
    return (
        ActionProposal(
            action_id=f"music-{session_id}",
            session_id=session_id,
            action_type=ActionType.PLAY_MUSIC,
            payload=MusicActionPayload(track_id="calm_piano_01"),
            proposed_at=proposed_at,
            expires_at=expiry,
        ),
        ActionProposal(
            action_id=f"ac-{session_id}",
            session_id=session_id,
            action_type=ActionType.SET_AC,
            payload=ACActionPayload(
                device_id="living_room_ac_mock",
                mode=ACMode.HEAT,
                target_temperature=24,
                duration_minutes=30,
            ),
            proposed_at=proposed_at,
            expires_at=expiry,
        ),
    )
