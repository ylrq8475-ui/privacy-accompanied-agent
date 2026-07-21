from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.app.orchestrator import InvalidOperation, MockScenario, Orchestrator
from backend.app.schemas.actions import AuthorizationStatus
from backend.app.schemas.step3 import StateLabel
from backend.app.state_machine import DemoState


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def ready_for_authorization(orchestrator: Orchestrator):
    session = orchestrator.start_session()
    return orchestrator.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)


class OrchestratorTests(unittest.TestCase):
    def test_malicious_step3_prose_cannot_execute_or_skip_clarification(self) -> None:
        orchestrator = Orchestrator()
        session = orchestrator.start_session()
        prose = session.step3_output.recommendation_reason[0].text
        self.assertIn("execute=true", prose)
        self.assertEqual(session.state, DemoState.CLARIFICATION_REQUIRED)
        self.assertIsNone(session.music_action)
        self.assertEqual(orchestrator.music.executed_action_ids, [])
        self.assertEqual(orchestrator.ac.executed_action_ids, [])

    def test_clarification_creates_independent_pending_actions(self) -> None:
        orchestrator = Orchestrator()
        session = ready_for_authorization(orchestrator)
        self.assertEqual(session.state, DemoState.WAITING_MUSIC_AUTHORIZATION)
        self.assertNotEqual(session.music_action.action_id, session.ac_action.action_id)
        self.assertEqual(
            session.authorizations[session.music_action.action_id].authorization_status,
            AuthorizationStatus.PENDING,
        )
        self.assertEqual(
            session.authorizations[session.ac_action.action_id].authorization_status,
            AuthorizationStatus.PENDING,
        )

    def test_rejecting_music_still_allows_ac_and_ac_does_not_trigger_music(self) -> None:
        orchestrator = Orchestrator()
        session = ready_for_authorization(orchestrator)
        orchestrator.authorize(session.session_id, session.music_action.action_id, False)
        self.assertEqual(session.state, DemoState.WAITING_AC_AUTHORIZATION)
        self.assertEqual(orchestrator.music.executed_action_ids, [])
        orchestrator.authorize(session.session_id, session.ac_action.action_id, True)
        self.assertEqual(session.state, DemoState.COMPLETED)
        self.assertEqual(orchestrator.music.executed_action_ids, [])
        self.assertEqual(orchestrator.ac.executed_action_ids, [session.ac_action.action_id])

    def test_wrong_action_id_is_rejected_without_execution(self) -> None:
        orchestrator = Orchestrator()
        session = ready_for_authorization(orchestrator)
        with self.assertRaises(InvalidOperation):
            orchestrator.authorize(session.session_id, session.ac_action.action_id, True)
        self.assertEqual(session.state, DemoState.WAITING_MUSIC_AUTHORIZATION)
        self.assertEqual(orchestrator.music.executed_action_ids, [])
        self.assertEqual(orchestrator.ac.executed_action_ids, [])

    def test_expired_music_authorization_does_not_execute_and_continues_to_ac(self) -> None:
        clock = MutableClock()
        orchestrator = Orchestrator(clock=clock)
        session = ready_for_authorization(orchestrator)
        clock.value += timedelta(minutes=6)
        orchestrator.authorize(session.session_id, session.music_action.action_id, True)
        self.assertIn(DemoState.ACTION_EXPIRED, session.state_history)
        self.assertEqual(session.state, DemoState.WAITING_AC_AUTHORIZATION)
        self.assertEqual(orchestrator.music.executed_action_ids, [])
        self.assertEqual(
            session.authorizations[session.music_action.action_id].authorization_status,
            AuthorizationStatus.EXPIRED,
        )

    def test_mock_action_failure_is_audited_and_not_claimed_as_physical_success(self) -> None:
        orchestrator = Orchestrator()
        session = ready_for_authorization(orchestrator)
        orchestrator.authorize(
            session.session_id,
            session.music_action.action_id,
            True,
            simulate_failure=True,
        )
        self.assertIn(DemoState.ACTION_FAILED, session.state_history)
        self.assertEqual(orchestrator.music.executed_action_ids, [])
        self.assertEqual(session.state, DemoState.WAITING_AC_AUTHORIZATION)

    def test_failure_scenarios_reach_declared_exception_states(self) -> None:
        cases = (
            (MockScenario.CAMERA_FAILED, DemoState.CAMERA_FAILED),
            (MockScenario.ASR_FAILED, DemoState.ASR_FAILED),
            (MockScenario.MODEL_TIMEOUT, DemoState.MODEL_TIMEOUT),
            (MockScenario.API_FAILED, DemoState.API_FAILED),
            (MockScenario.PRIVACY_REJECTED, DemoState.PRIVACY_REJECTED),
        )
        for scenario, expected in cases:
            with self.subTest(scenario=scenario.value):
                orchestrator = Orchestrator()
                session = orchestrator.start_session(scenario)
                self.assertEqual(session.state, expected)

    def test_privacy_rejection_is_before_send_and_does_not_audit_raw_value(self) -> None:
        orchestrator = Orchestrator()
        session = orchestrator.start_session(MockScenario.PRIVACY_REJECTED)
        self.assertEqual(session.state, DemoState.PRIVACY_REJECTED)
        self.assertEqual(orchestrator.connector.sent_requests, [])
        serialized = "".join(
            event.model_dump_json()
            for event in orchestrator.audit_log.list_events(session.session_id)
        )
        self.assertNotIn("synthetic-forbidden-value", serialized)

    def test_every_state_change_has_one_transition_event(self) -> None:
        orchestrator = Orchestrator()
        session = ready_for_authorization(orchestrator)
        orchestrator.authorize(session.session_id, session.music_action.action_id, True)
        orchestrator.authorize(session.session_id, session.ac_action.action_id, True)
        transitions = [
            event
            for event in orchestrator.audit_log.list_events(session.session_id)
            if event.event_type == "STATE_TRANSITION"
        ]
        self.assertEqual(len(transitions), len(session.state_history) - 1)
        self.assertEqual(
            [event.payload["to_state"] for event in transitions],
            [state.value for state in session.state_history[1:]],
        )

    def test_local_actions_do_not_add_external_connector_requests(self) -> None:
        orchestrator = Orchestrator()
        session = ready_for_authorization(orchestrator)
        self.assertEqual(len(orchestrator.connector.sent_requests), 1)
        orchestrator.authorize(session.session_id, session.music_action.action_id, True)
        orchestrator.authorize(session.session_id, session.ac_action.action_id, True)
        self.assertEqual(len(orchestrator.connector.sent_requests), 1)


if __name__ == "__main__":
    unittest.main()
