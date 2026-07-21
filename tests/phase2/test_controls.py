from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.api import DemoASGIApp
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.actions import AuthorizationStatus, ExecutionStatus
from tests.phase1c.helpers import FixedClock
from tests.phase2.helpers import json_request


class Phase2ControlAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = SQLitePersistence(Path(self.temporary.name) / "demo.sqlite3")
        self.orchestrator = Orchestrator(
            clock=FixedClock(), persistence=self.store
        )
        self.app = DemoASGIApp(self.orchestrator)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_allowlisted_text_input_records_event_and_clarifies(self) -> None:
        session = self.orchestrator.start_session()
        status, snapshot = await json_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session.session_id}/text-input",
            {"text": "主要是身体累"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["state"], "WAITING_MUSIC_AUTHORIZATION")
        self.assertEqual(snapshot["interaction_source"], "TEXT")
        self.assertEqual(snapshot["last_text_input"], "主要是身体累")
        events = self.orchestrator.audit_log.list_events(session.session_id)
        self.assertEqual(
            sum(event.event_type == "USER_TEXT_INPUT" for event in events), 1
        )
        self.assertEqual(
            next(event for event in events if event.event_type == "USER_TEXT_INPUT").payload,
            {"text": "主要是身体累", "input_source": "TEXT", "mock": True},
        )

    async def test_text_input_contract_rejects_invalid_shapes_without_progress(self) -> None:
        invalid = (
            {},
            {"text": "任意自由文本"},
            {"text": 1},
            {"text": "主要是身体累", "answer": "PHYSICAL_FATIGUE"},
        )
        for body in invalid:
            session = self.orchestrator.start_session()
            with self.subTest(body=body):
                status, response = await json_request(
                    self.app,
                    "POST",
                    f"/v1/mock/sessions/{session.session_id}/text-input",
                    body,
                )
                self.assertEqual(status, 422)
                self.assertEqual(response["error"], "VALIDATION_ERROR")
                self.assertEqual(session.state.value, "CLARIFICATION_REQUIRED")

    async def test_reset_is_idempotent_closes_session_and_never_executes_pending(self) -> None:
        session = self.orchestrator.start_session()
        await json_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session.session_id}/text-input",
            {"text": "主要是身体累"},
        )
        music_id = session.music_action.action_id
        ac_id = session.ac_action.action_id

        status, snapshot = await json_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session.session_id}/reset",
        )
        self.assertEqual(status, 200)
        self.assertFalse(snapshot["active"])
        self.assertFalse(snapshot["person_present"])
        self.assertEqual(self.orchestrator.music.executed_action_ids, [])
        self.assertEqual(self.orchestrator.ac.executed_action_ids, [])
        for action_id in (music_id, ac_id):
            action = self.store.get_action(action_id)
            self.assertEqual(action.authorization_status, AuthorizationStatus.PENDING)
            self.assertEqual(action.execution_status, ExecutionStatus.NOT_STARTED)

        status, response = await json_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session.session_id}/actions/{music_id}/authorization",
            {"approved": True},
        )
        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "INVALID_OPERATION")

        status, _ = await json_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session.session_id}/reset",
        )
        self.assertEqual(status, 200)
        event_types = [
            event.event_type
            for event in self.orchestrator.audit_log.list_events(session.session_id)
        ]
        self.assertEqual(event_types.count("PERSON_LEFT"), 1)
        self.assertEqual(event_types.count("DEMO_RESET"), 1)

    async def test_reset_request_rejects_extra_fields(self) -> None:
        session = self.orchestrator.start_session()
        status, response = await json_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session.session_id}/reset",
            {"delete_memory": True},
        )
        self.assertEqual(status, 422)
        self.assertEqual(response["error"], "VALIDATION_ERROR")
        self.assertTrue(session.active)

    async def test_refresh_reads_pending_state_without_executing_actions(self) -> None:
        session = self.orchestrator.start_session()
        await json_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session.session_id}/text-input",
            {"text": "主要是身体累"},
        )

        status, refreshed = await json_request(
            self.app, "GET", f"/v1/mock/sessions/{session.session_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(refreshed["state"], "WAITING_MUSIC_AUTHORIZATION")
        self.assertTrue(refreshed["active"])

        for proposal_key in ("music_action", "ac_action"):
            action_id = refreshed[proposal_key]["action_id"]
            action_status, action = await json_request(
                self.app, "GET", f"/v1/actions/{action_id}"
            )
            self.assertEqual(action_status, 200)
            self.assertEqual(action["authorization_status"], "PENDING")
            self.assertEqual(action["execution_status"], "NOT_STARTED")
        self.assertEqual(self.orchestrator.music.executed_action_ids, [])
        self.assertEqual(self.orchestrator.ac.executed_action_ids, [])


if __name__ == "__main__":
    unittest.main()
