from __future__ import annotations

import unittest
from datetime import timedelta

from pydantic import ValidationError

from backend.app.policy.authorization import ensure_action_executable, ensure_authorization_transition
from backend.app.schemas.actions import (
    ACActionPayload,
    ACMode,
    ActionAuthorization,
    ActionExecution,
    ActionProposal,
    ActionResult,
    ActionType,
    AuthorizationStatus,
    ExecutionStatus,
    MusicActionPayload,
)
from tests.helpers import LATER, NOW, ac_proposal, approved_authorization, music_proposal, pending_authorization


class ActionContractTests(unittest.TestCase):
    def test_valid_music_proposal(self) -> None:
        self.assertEqual(music_proposal().action_id, "music-001")

    def test_valid_ac_proposal_has_independent_id(self) -> None:
        music = music_proposal()
        ac = ac_proposal()
        self.assertNotEqual(music.action_id, ac.action_id)
        self.assertEqual(ac.action_type, ActionType.SET_AC)

    def test_wrong_action_id_prefix_is_rejected(self) -> None:
        with self.subTest("music"):
            with self.assertRaises(ValidationError):
                ActionProposal(
                    action_id="ac-wrong",
                    session_id="session-001",
                    action_type=ActionType.PLAY_MUSIC,
                    payload=MusicActionPayload(track_id="calm_piano_01"),
                    proposed_at=NOW,
                    expires_at=LATER,
                )
        with self.subTest("ac"):
            with self.assertRaises(ValidationError):
                ActionProposal(
                    action_id="music-wrong",
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

    def test_action_payload_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ActionProposal(
                action_id="music-001",
                session_id="session-001",
                action_type=ActionType.PLAY_MUSIC,
                payload=ACActionPayload(
                    device_id="living_room_ac",
                    mode=ACMode.HEAT,
                    target_temperature=24,
                    duration_minutes=30,
                ),
                proposed_at=NOW,
                expires_at=LATER,
            )

    def test_proposal_extra_field_is_rejected(self) -> None:
        data = music_proposal().model_dump()
        data["authorization_status"] = "APPROVED"
        with self.assertRaises(ValidationError):
            ActionProposal.model_validate(data)

    def test_invalid_authorization_status_is_rejected(self) -> None:
        data = pending_authorization().model_dump()
        data["authorization_status"] = "GRANTED"
        with self.assertRaises(ValidationError):
            ActionAuthorization.model_validate(data)

    def test_approved_authorization_requires_decision_time(self) -> None:
        with self.assertRaises(ValidationError):
            ActionAuthorization(
                action_id="music-001",
                action_type=ActionType.PLAY_MUSIC,
                authorization_status=AuthorizationStatus.APPROVED,
                decided_at=None,
                expires_at=LATER,
            )

    def test_approved_authorization_expiry_must_follow_decision(self) -> None:
        with self.assertRaises(ValidationError):
            ActionAuthorization(
                action_id="music-001",
                action_type=ActionType.PLAY_MUSIC,
                authorization_status=AuthorizationStatus.APPROVED,
                decided_at=NOW,
                expires_at=NOW,
            )

    def test_valid_execution_lifecycle_shapes(self) -> None:
        running = ActionExecution(
            action_id="music-001",
            action_type=ActionType.PLAY_MUSIC,
            execution_status=ExecutionStatus.RUNNING,
            started_at=NOW,
            finished_at=None,
        )
        succeeded = ActionExecution(
            action_id="music-001",
            action_type=ActionType.PLAY_MUSIC,
            execution_status=ExecutionStatus.SUCCEEDED,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=1),
        )
        self.assertEqual(running.execution_status, ExecutionStatus.RUNNING)
        self.assertEqual(succeeded.execution_status, ExecutionStatus.SUCCEEDED)

    def test_invalid_execution_timestamps_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ActionExecution(
                action_id="music-001",
                action_type=ActionType.PLAY_MUSIC,
                execution_status=ExecutionStatus.SUCCEEDED,
                started_at=NOW,
                finished_at=None,
            )

    def test_valid_terminal_result(self) -> None:
        result = ActionResult(
            action_id="music-001",
            action_type=ActionType.PLAY_MUSIC,
            execution_status=ExecutionStatus.SUCCEEDED,
            result={"synthetic": True},
            completed_at=NOW,
        )
        self.assertTrue(result.result["synthetic"])

    def test_nonterminal_result_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ActionResult(
                action_id="music-001",
                action_type=ActionType.PLAY_MUSIC,
                execution_status=ExecutionStatus.RUNNING,
                result={},
                completed_at=NOW,
            )

    def test_duplicate_authorization_is_rejected(self) -> None:
        approved = approved_authorization()
        with self.assertRaises(ValueError):
            ensure_authorization_transition(approved, approved)

    def test_expired_or_mismatched_authorization_is_rejected(self) -> None:
        with self.subTest("expired"):
            with self.assertRaises(ValueError):
                ensure_action_executable(music_proposal(), approved_authorization(), LATER)
        with self.subTest("id"):
            wrong_id = approved_authorization().model_copy(update={"action_id": "music-002"})
            with self.assertRaises(ValueError):
                ensure_action_executable(music_proposal(), wrong_id, NOW)
        with self.subTest("type"):
            wrong_type = approved_authorization().model_copy(update={"action_type": ActionType.SET_AC})
            with self.assertRaises(ValueError):
                ensure_action_executable(music_proposal(), wrong_type, NOW)

    def test_nonapproved_action_is_rejected(self) -> None:
        rejected = ActionAuthorization(
            action_id="music-001",
            action_type=ActionType.PLAY_MUSIC,
            authorization_status=AuthorizationStatus.REJECTED,
            decided_at=NOW,
            expires_at=LATER,
        )
        with self.assertRaises(ValueError):
            ensure_action_executable(music_proposal(), rejected, NOW)


if __name__ == "__main__":
    unittest.main()
