from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.actions import ExecutionStatus
from backend.app.schemas.step3 import StateLabel
from backend.app.state_machine import DemoState
from tests.phase1c.helpers import FixedClock


class PersistentEndToEndTests(unittest.TestCase):
    def test_persistent_mock_scenario_succeeds_five_out_of_five(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "demo.sqlite3"
            successful_sessions: list[str] = []
            for sample_number in range(1, 6):
                with self.subTest(sample=sample_number):
                    orchestrator = Orchestrator(
                        clock=FixedClock(), persistence=SQLitePersistence(path)
                    )
                    session = orchestrator.start_session()
                    orchestrator.clarify(
                        session.session_id, StateLabel.PHYSICAL_FATIGUE
                    )
                    orchestrator.authorize(
                        session.session_id, session.music_action.action_id, True
                    )
                    orchestrator.authorize(
                        session.session_id, session.ac_action.action_id, True
                    )
                    self.assertEqual(session.state, DemoState.COMPLETED)
                    self.assertNotEqual(
                        session.music_action.action_id, session.ac_action.action_id
                    )
                    self.assertEqual(
                        orchestrator.music.executed_action_ids,
                        [session.music_action.action_id],
                    )
                    self.assertEqual(
                        orchestrator.ac.executed_action_ids,
                        [session.ac_action.action_id],
                    )
                    for action_id in (
                        session.music_action.action_id,
                        session.ac_action.action_id,
                    ):
                        result = session.results[action_id].result
                        self.assertTrue(result["mock"])
                        self.assertFalse(result["physical_action_performed"])
                    persistent_events = orchestrator.persistence.list_audits(
                        session.session_id
                    )
                    transition_count = sum(
                        event.event_type == "STATE_TRANSITION"
                        for event in persistent_events
                    )
                    self.assertEqual(
                        transition_count, len(session.state_history) - 1
                    )
                    event_types = [event.event_type for event in persistent_events]
                    self.assertEqual(event_types.count("ACTION_PERSISTED"), 2)
                    self.assertEqual(
                        event_types.count("ACTION_AUTHORIZATION_UPDATED"), 2
                    )
                    self.assertEqual(event_types.count("ACTION_EXECUTION_UPDATED"), 4)
                    successful_sessions.append(session.session_id)

            reopened = SQLitePersistence(path)
            actions = reopened.list_actions()
            self.assertEqual(len(successful_sessions), 5)
            self.assertEqual(len(set(successful_sessions)), 5)
            self.assertEqual(len(actions), 10)
            self.assertTrue(
                all(item.execution_status is ExecutionStatus.SUCCEEDED for item in actions)
            )
            for item in actions:
                keys = set(item.payload.model_dump(mode="json"))
                expected = (
                    {"track_id"}
                    if item.action_type.value == "PLAY_MUSIC"
                    else {
                        "device_id",
                        "mode",
                        "target_temperature",
                        "duration_minutes",
                    }
                )
                self.assertEqual(keys, expected)

            with closing(sqlite3.connect(path)) as connection:
                action_payloads = [
                    row[0] for row in connection.execute("SELECT payload FROM actions")
                ]
                audit_columns = [
                    row[1] for row in connection.execute("PRAGMA table_info(audit_log)")
                ]
            for payload in action_payloads:
                self.assertNotIn("recommendation_reason", payload)
                self.assertNotIn("raw_audio", payload)
                self.assertNotIn("raw_video", payload)
                json.loads(payload)
            self.assertNotIn("payload", audit_columns)

            database_bytes = path.read_bytes().lower()
            for forbidden in (
                b"raw_audio",
                b"raw_video",
                b"synthetic user says",
                b"recommendation_reason",
                b"execute=true",
            ):
                self.assertNotIn(forbidden, database_bytes)


if __name__ == "__main__":
    unittest.main()
