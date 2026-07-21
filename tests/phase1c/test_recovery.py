from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from backend.app.mocks import MockAC, MockMusic
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.actions import (
    ActionResult,
    AuthorizationStatus,
    ExecutionStatus,
)
from tests.phase1c.helpers import FixedClock, NOW, proposals


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "demo.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pending_restart_stays_not_started_and_never_executes(self) -> None:
        store = SQLitePersistence(self.path)
        music_action, ac_action = proposals()
        store.create_actions((music_action, ac_action))
        music = MockMusic()
        ac = MockAC()
        restarted = Orchestrator(
            clock=FixedClock(NOW + timedelta(minutes=1)),
            persistence=SQLitePersistence(self.path),
            music=music,
            ac=ac,
        )
        restarted.initialize_persistence()
        recovered = restarted.persistence.list_actions()
        self.assertTrue(
            all(item.authorization_status is AuthorizationStatus.PENDING for item in recovered)
        )
        self.assertTrue(
            all(item.execution_status is ExecutionStatus.NOT_STARTED for item in recovered)
        )
        self.assertEqual(music.executed_action_ids, [])
        self.assertEqual(ac.executed_action_ids, [])

    def test_valid_approved_restart_is_retained_without_execution(self) -> None:
        store = SQLitePersistence(self.path)
        music_action, _ = proposals()
        store.create_actions((music_action,))
        store.update_authorization(music_action.action_id, AuthorizationStatus.APPROVED, NOW)
        music = MockMusic()
        restarted = Orchestrator(
            clock=FixedClock(NOW + timedelta(minutes=1)),
            persistence=SQLitePersistence(self.path),
            music=music,
        )
        restarted.initialize_persistence()
        record = restarted.get_persisted_action(music_action.action_id)
        self.assertEqual(record.authorization_status, AuthorizationStatus.APPROVED)
        self.assertEqual(record.execution_status, ExecutionStatus.NOT_STARTED)
        self.assertEqual(music.executed_action_ids, [])

    def test_expired_approved_restart_becomes_expired_without_execution(self) -> None:
        store = SQLitePersistence(self.path)
        music_action, _ = proposals()
        store.create_actions((music_action,))
        store.update_authorization(music_action.action_id, AuthorizationStatus.APPROVED, NOW)
        music = MockMusic()
        restarted = Orchestrator(
            clock=FixedClock(NOW + timedelta(minutes=6)),
            persistence=SQLitePersistence(self.path),
            music=music,
        )
        restarted.initialize_persistence()
        record = restarted.get_persisted_action(music_action.action_id)
        self.assertEqual(record.authorization_status, AuthorizationStatus.EXPIRED)
        self.assertEqual(record.execution_status, ExecutionStatus.NOT_STARTED)
        self.assertEqual(music.executed_action_ids, [])

    def test_running_restart_becomes_failed_interrupted_and_is_not_retried(self) -> None:
        store = SQLitePersistence(self.path)
        music_action, _ = proposals()
        store.create_actions((music_action,))
        store.update_authorization(music_action.action_id, AuthorizationStatus.APPROVED, NOW)
        store.mark_running(music_action.action_id)
        music = MockMusic()
        restarted = Orchestrator(
            clock=FixedClock(NOW + timedelta(minutes=1)),
            persistence=SQLitePersistence(self.path),
            music=music,
        )
        restarted.initialize_persistence()
        restarted.initialize_persistence()
        record = restarted.get_persisted_action(music_action.action_id)
        self.assertEqual(record.execution_status, ExecutionStatus.FAILED)
        self.assertEqual(record.result["code"], "INTERRUPTED_BY_RESTART")
        self.assertFalse(record.result["physical_action_performed"])
        self.assertEqual(music.executed_action_ids, [])
        audits = restarted.persistence.list_audits(music_action.session_id)
        self.assertEqual(
            sum(item.event_type == "ACTION_RECOVERY_INTERRUPTED" for item in audits), 1
        )

    def test_terminal_success_rejection_and_failure_survive_reopen(self) -> None:
        store = SQLitePersistence(self.path)
        succeeded, rejected = proposals(session_id="session-terminals-001")
        failed, = proposals(session_id="session-terminals-002")[:1]
        store.create_actions((succeeded, rejected, failed))
        store.update_authorization(succeeded.action_id, AuthorizationStatus.APPROVED, NOW)
        store.mark_running(succeeded.action_id)
        store.finish_action(
            ActionResult(
                action_id=succeeded.action_id,
                action_type=succeeded.action_type,
                execution_status=ExecutionStatus.SUCCEEDED,
                result={"mock": True, "physical_action_performed": False},
                completed_at=NOW,
            )
        )
        store.update_authorization(rejected.action_id, AuthorizationStatus.REJECTED, NOW)
        store.update_authorization(failed.action_id, AuthorizationStatus.APPROVED, NOW)
        store.mark_running(failed.action_id)
        store.finish_action(
            ActionResult(
                action_id=failed.action_id,
                action_type=failed.action_type,
                execution_status=ExecutionStatus.FAILED,
                result={
                    "code": "MOCK_EXECUTION_FAILED",
                    "mock": True,
                    "physical_action_performed": False,
                },
                completed_at=NOW,
            )
        )
        reopened = SQLitePersistence(self.path)
        self.assertEqual(
            reopened.get_action(succeeded.action_id).execution_status,
            ExecutionStatus.SUCCEEDED,
        )
        self.assertEqual(
            reopened.get_action(rejected.action_id).authorization_status,
            AuthorizationStatus.REJECTED,
        )
        self.assertEqual(
            reopened.get_action(failed.action_id).execution_status,
            ExecutionStatus.FAILED,
        )


if __name__ == "__main__":
    unittest.main()
