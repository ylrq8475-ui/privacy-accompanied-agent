from __future__ import annotations

import unittest

from backend.app.policy.clarification import evaluate_clarification
from backend.app.schemas.step3 import StateHypothesis
from backend.app.state_machine import DemoState, InvalidStateTransition, ensure_state_transition


def hypothesis(confidence: float) -> StateHypothesis:
    return StateHypothesis(label="OTHER", confidence=confidence, evidence=[])


class ClarificationPolicyTests(unittest.TestCase):
    def test_low_top_confidence_requires_clarification(self) -> None:
        decision = evaluate_clarification([hypothesis(0.60), hypothesis(0.20)])
        self.assertTrue(decision.required)
        self.assertIn("TOP_CONFIDENCE_BELOW_THRESHOLD", decision.reason_codes)

    def test_narrow_top_two_gap_requires_clarification(self) -> None:
        decision = evaluate_clarification([hypothesis(0.80), hypothesis(0.60)])
        self.assertTrue(decision.required)
        self.assertIn("TOP_TWO_GAP_BELOW_THRESHOLD", decision.reason_codes)

    def test_confident_separated_candidate_does_not_require_clarification(self) -> None:
        decision = evaluate_clarification([hypothesis(0.80), hypothesis(0.40)])
        self.assertFalse(decision.required)

    def test_empty_hypotheses_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_clarification([])


class StateMachineTests(unittest.TestCase):
    def test_declared_transition_is_allowed(self) -> None:
        self.assertEqual(
            ensure_state_transition(DemoState.IDLE, DemoState.PERSON_DETECTED),
            DemoState.PERSON_DETECTED,
        )

    def test_future_state_cannot_be_skipped(self) -> None:
        with self.assertRaises(InvalidStateTransition):
            ensure_state_transition(DemoState.IDLE, DemoState.MUSIC_EXECUTED)

    def test_terminal_state_cannot_restart(self) -> None:
        with self.assertRaises(InvalidStateTransition):
            ensure_state_transition(DemoState.COMPLETED, DemoState.IDLE)


if __name__ == "__main__":
    unittest.main()
