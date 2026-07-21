from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.api import DemoASGIApp
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from tests.phase1c.helpers import FixedClock
from tests.phase2.helpers import json_request


class Phase2ControlEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_control_flow_completes_five_out_of_five(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "demo.sqlite3"
            session_ids: list[str] = []
            for sample_number in range(1, 6):
                with self.subTest(sample=sample_number):
                    orchestrator = Orchestrator(
                        clock=FixedClock(),
                        persistence=SQLitePersistence(database),
                    )
                    app = DemoASGIApp(orchestrator)
                    status, session = await json_request(
                        app,
                        "POST",
                        "/v1/mock/sessions",
                        {"scenario": "success"},
                    )
                    self.assertEqual(status, 201)
                    self.assertTrue(session["person_present"])
                    session_id = session["session_id"]

                    status, session = await json_request(
                        app,
                        "POST",
                        f"/v1/mock/sessions/{session_id}/text-input",
                        {"text": "主要是身体累"},
                    )
                    self.assertEqual(status, 200)
                    music_id = session["music_action"]["action_id"]
                    ac_id = session["ac_action"]["action_id"]
                    self.assertNotEqual(music_id, ac_id)

                    status, session = await json_request(
                        app,
                        "POST",
                        f"/v1/mock/sessions/{session_id}/actions/{music_id}/authorization",
                        {"approved": True},
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(session["state"], "WAITING_AC_AUTHORIZATION")

                    status, session = await json_request(
                        app,
                        "POST",
                        f"/v1/mock/sessions/{session_id}/actions/{ac_id}/authorization",
                        {"approved": True},
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(session["state"], "COMPLETED")
                    for action_id in (music_id, ac_id):
                        result = session["results"][action_id]["result"]
                        self.assertTrue(result["mock"])
                        self.assertFalse(result["physical_action_performed"])

                    status, events = await json_request(
                        app, "GET", f"/v1/mock/sessions/{session_id}/events"
                    )
                    self.assertEqual(status, 200)
                    internet_event = next(
                        item
                        for item in events["events"]
                        if item["event_type"] == "INTERNET_MOCK_REQUEST"
                    )
                    self.assertEqual(internet_event["network_scope"], "INTERNET")
                    self.assertEqual(
                        internet_event["payload"]["outbound_payload"],
                        {"city_code": "310000"},
                    )
                    session_ids.append(session_id)

            self.assertEqual(len(session_ids), 5)
            self.assertEqual(len(set(session_ids)), 5)


if __name__ == "__main__":
    unittest.main()
