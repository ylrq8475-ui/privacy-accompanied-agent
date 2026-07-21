from __future__ import annotations

import unittest

from backend.app.orchestrator import Orchestrator
from backend.app.schemas.step3 import StateLabel
from backend.app.state_machine import DemoState


EXPECTED_SUCCESS_CHAIN = [
    DemoState.IDLE,
    DemoState.PERSON_DETECTED,
    DemoState.CONTEXT_READY,
    DemoState.LISTENING,
    DemoState.MOOD_ANALYSIS,
    DemoState.CLARIFICATION_REQUIRED,
    DemoState.USER_CLARIFIED,
    DemoState.MEMORY_RETRIEVED,
    DemoState.ACTION_PROPOSED,
    DemoState.WAITING_MUSIC_AUTHORIZATION,
    DemoState.MUSIC_AUTHORIZED,
    DemoState.MUSIC_EXECUTED,
    DemoState.WAITING_AC_AUTHORIZATION,
    DemoState.AC_AUTHORIZED,
    DemoState.AC_EXECUTED,
    DemoState.COMPLETED,
]


class FixedScenarioEndToEndTests(unittest.TestCase):
    def test_fixed_mock_scenario_succeeds_five_out_of_five(self) -> None:
        successful_sessions: list[str] = []
        for sample_number in range(1, 6):
            with self.subTest(sample=sample_number):
                orchestrator = Orchestrator()
                session = orchestrator.start_session()
                orchestrator.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
                orchestrator.authorize(
                    session.session_id, session.music_action.action_id, True
                )
                orchestrator.authorize(session.session_id, session.ac_action.action_id, True)

                self.assertEqual(session.state, DemoState.COMPLETED)
                self.assertEqual(session.state_history, EXPECTED_SUCCESS_CHAIN)
                self.assertNotEqual(session.music_action.action_id, session.ac_action.action_id)
                self.assertEqual(
                    orchestrator.music.executed_action_ids,
                    [session.music_action.action_id],
                )
                self.assertEqual(
                    orchestrator.ac.executed_action_ids,
                    [session.ac_action.action_id],
                )
                self.assertTrue(
                    session.results[session.music_action.action_id].result["mock"]
                )
                self.assertFalse(
                    session.results[session.music_action.action_id].result[
                        "physical_action_performed"
                    ]
                )
                self.assertTrue(session.results[session.ac_action.action_id].result["mock"])
                self.assertFalse(
                    session.results[session.ac_action.action_id].result[
                        "physical_action_performed"
                    ]
                )
                successful_sessions.append(session.session_id)

        self.assertEqual(len(successful_sessions), 5)
        self.assertEqual(len(set(successful_sessions)), 5)


if __name__ == "__main__":
    unittest.main()
