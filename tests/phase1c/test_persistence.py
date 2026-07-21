from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence, canonical_payload_digest
from backend.app.schemas.events import NetworkScope
from backend.app.schemas.persistence import MemoryConfirmationRequest
from backend.app.schemas.step3 import StateLabel
from tests.phase1c.helpers import FixedClock, NOW


def confirmed_request() -> MemoryConfirmationRequest:
    return MemoryConfirmationRequest(
        context="workday_evening", preference="calm_piano", confirmed=True
    )


class SQLitePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "nested" / "demo.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_initialization_is_lazy_and_migration_is_idempotent(self) -> None:
        first = SQLitePersistence(self.database_path)
        self.assertFalse(self.database_path.exists())
        first.initialize()
        first.initialize()
        second = SQLitePersistence(self.database_path)
        second.initialize()

        self.assertEqual(second.migration_versions(), [1, 2, 3, 4, 5])
        with closing(sqlite3.connect(self.database_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            migration_count = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
        self.assertEqual(
            tables,
            {
                "schema_migrations",
                "memories",
                "actions",
                "audit_log",
                "weather_cache",
                "user_preferences",
                "emotion_summaries",
            },
        )
        self.assertEqual(migration_count, 5)

    def test_confirmed_memory_survives_reopen_and_delete_survives_reopen(self) -> None:
        first = SQLitePersistence(self.database_path)
        memory = first.confirm_memory(confirmed_request(), NOW)
        reopened = SQLitePersistence(self.database_path)
        self.assertEqual(reopened.list_memories(), [memory])
        self.assertTrue(reopened.delete_memory(memory.memory_id))
        self.assertEqual(SQLitePersistence(self.database_path).list_memories(), [])

    def test_unconfirmed_or_malformed_memory_contracts_are_rejected(self) -> None:
        invalid = (
            {"context": "workday_evening", "preference": "calm_piano", "confirmed": False},
            {"context": "workday_evening", "confirmed": True},
            {
                "context": "workday_evening",
                "preference": "calm_piano",
                "confirmed": True,
                "user_id": "not-supported",
            },
            {"context": "workday_evening", "preference": "calm_piano", "confirmed": 1},
            {"context": "weekend", "preference": "calm_piano", "confirmed": True},
        )
        store = SQLitePersistence(self.database_path)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                MemoryConfirmationRequest.model_validate(value)
        self.assertEqual(store.list_memories(), [])

    def test_deleted_memory_is_not_visible_to_decision_and_mock_fallback_is_not_used(self) -> None:
        store = SQLitePersistence(self.database_path)
        memory = store.confirm_memory(confirmed_request(), NOW)
        store.delete_memory(memory.memory_id)

        class ForbiddenFallback:
            def retrieve_confirmed_preferences(self):
                raise AssertionError("persistent mode must not fall back to MockMemory")

        orchestrator = Orchestrator(
            clock=FixedClock(), persistence=SQLitePersistence(self.database_path), memory=ForbiddenFallback()
        )
        session = orchestrator.start_session()
        orchestrator.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
        retrieved = next(
            event
            for event in orchestrator.audit_log.list_events(session.session_id)
            if event.event_type == "STATE_TRANSITION"
            and event.payload["to_state"] == "MEMORY_RETRIEVED"
        )
        self.assertFalse(retrieved.payload["details"]["confirmed"])
        self.assertEqual(retrieved.payload["details"]["preferences"], [])

    def test_audit_stores_only_canonical_digest_and_metadata(self) -> None:
        store = SQLitePersistence(self.database_path)
        payload = {"z": 2, "a": {"value": "synthetic"}}
        record = store.append_audit(
            audit_id="audit-001",
            session_id="session-001",
            event_type="TEST_EVENT",
            network_scope=NetworkScope.LOCAL,
            privacy_result="PASSED",
            payload=payload,
            created_at=NOW,
        )
        self.assertEqual(record.payload_digest, canonical_payload_digest(payload))
        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = [
                row[1] for row in connection.execute("PRAGMA table_info(audit_log)")
            ]
            stored = connection.execute("SELECT * FROM audit_log").fetchone()
        self.assertEqual(
            columns,
            [
                "audit_id",
                "session_id",
                "event_type",
                "network_scope",
                "privacy_result",
                "payload_digest",
                "created_at",
            ],
        )
        self.assertNotIn(json.dumps(payload), "".join(str(value) for value in stored))


if __name__ == "__main__":
    unittest.main()
