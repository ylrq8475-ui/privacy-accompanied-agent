from __future__ import annotations

import unittest

from scripts.phase5_acceptance import run_recovery


class Phase5RecoveryTests(unittest.TestCase):
    def test_two_short_lived_processes_recover_without_execution(self) -> None:
        result = run_recovery()
        self.assertTrue(result.passed)
        self.assertEqual(result.auto_execution_count, 0)
        self.assertEqual(result.states["pending"], "PENDING/NOT_STARTED")
        self.assertEqual(result.states["approved"], "APPROVED/NOT_STARTED")
        self.assertEqual(result.states["expired"], "EXPIRED/NOT_STARTED")
        self.assertEqual(result.states["running"], "APPROVED/FAILED")
        self.assertEqual(result.states["terminal"], "APPROVED/SUCCEEDED")
        self.assertTrue(result.console_reloaded)
        self.assertTrue(result.connector_recreated)


if __name__ == "__main__":
    unittest.main()
