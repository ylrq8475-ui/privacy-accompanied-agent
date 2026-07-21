from __future__ import annotations

import unittest

from scripts.phase5_acceptance import (
    _sample_sync,
    run_degraded_e2e,
    run_fault_matrix,
    run_fixed_e2e,
)
from scripts.phase5_metrics import summarize


class Phase5AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_mock_e2e_is_five_out_of_five(self) -> None:
        result = await run_fixed_e2e(5)
        self.assertTrue(result.passed)
        self.assertEqual(result.success_count, 5)
        self.assertEqual(result.unique_session_count, 5)
        self.assertFalse(result.details["ac_physical_action_performed"])

    async def test_model_unavailable_e2e_is_five_out_of_five(self) -> None:
        result = await run_degraded_e2e(5)
        self.assertTrue(result.passed)
        self.assertEqual(result.success_count, 5)
        self.assertEqual(result.unique_session_count, 5)
        self.assertEqual(result.details["model_fallback_count"], 5)

    def test_fault_matrix_exercises_every_declared_safe_fallback(self) -> None:
        results = run_fault_matrix()
        self.assertEqual(len(results), 14)
        self.assertTrue(all(result.passed for result in results))
        self.assertEqual(len({result.scenario for result in results}), 14)

    def test_sampler_records_all_attempts_and_failures(self) -> None:
        def operation(index: int) -> str:
            if index in {3, 11}:
                raise RuntimeError("synthetic failure")
            return "EXPECTED"

        samples = _sample_sync(20, operation)
        summary = summarize(
            "SYNTHETIC_INTERFACE",
            "TEST_DOUBLE",
            samples,
            minimum_success_count=18,
        )
        self.assertEqual(summary.count, 20)
        self.assertEqual(summary.success_count, 18)
        self.assertEqual(summary.failure_count, 2)
        self.assertIsNotNone(summary.p95_ms)
        self.assertTrue(summary.passed)


if __name__ == "__main__":
    unittest.main()
