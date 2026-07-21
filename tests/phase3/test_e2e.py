from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.adapters import PerceptionSource, VisionAdapter
from backend.app.api import DemoASGIApp
from backend.app.live import LiveCoordinator
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from tests.phase1c.helpers import FixedClock
from tests.phase2.helpers import json_request
from tests.phase3.helpers import FailingAudio, FailingStep3


class Phase3DegradedEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_degraded_live_chain_completes_five_out_of_five(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "demo.sqlite3"
            session_ids: list[str] = []
            for sample in range(1, 6):
                with self.subTest(sample=sample):
                    orchestrator = Orchestrator(
                        clock=FixedClock(), persistence=SQLitePersistence(database)
                    )
                    vision = VisionAdapter(
                        observers=[
                            (PerceptionSource.STATIC_SYNTHETIC, lambda: (True, None))
                        ]
                    )
                    live = LiveCoordinator(
                        orchestrator,
                        vision=vision,
                        audio=FailingAudio(),
                        step3=FailingStep3(),
                        confirmation_samples=2,
                    )
                    app = DemoASGIApp(orchestrator, live=live)

                    await json_request(app, "POST", "/v1/live/perception/poll")
                    status, result = await json_request(
                        app, "POST", "/v1/live/perception/poll"
                    )
                    self.assertEqual(status, 200)
                    session = result["session"]
                    session_id = session["session_id"]
                    self.assertEqual(session["state"], "LISTENING")
                    self.assertEqual(session["perception_source"], "STATIC_SYNTHETIC")

                    status, session = await json_request(
                        app,
                        "POST",
                        f"/v1/live/sessions/{session_id}/transcript",
                        {"text": "今天有点累"},
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(session["state"], "CLARIFICATION_REQUIRED")
                    self.assertEqual(session["model_source"], "RULE_FALLBACK")

                    status, session = await json_request(
                        app,
                        "POST",
                        f"/v1/live/sessions/{session_id}/clarifications",
                        {"answer": "PHYSICAL_FATIGUE"},
                    )
                    self.assertEqual(status, 200)
                    music_id = session["music_action"]["action_id"]
                    ac_id = session["ac_action"]["action_id"]
                    self.assertNotEqual(music_id, ac_id)

                    for action_id in (music_id, ac_id):
                        status, session = await json_request(
                            app,
                            "POST",
                            f"/v1/live/sessions/{session_id}/actions/{action_id}/authorization",
                            {"approved": True},
                        )
                        self.assertEqual(status, 200)
                    self.assertEqual(session["state"], "COMPLETED")
                    for action_id in (music_id, ac_id):
                        self.assertFalse(
                            session["results"][action_id]["result"]["physical_action_performed"]
                        )
                    session_ids.append(session_id)

            self.assertEqual(len(session_ids), 5)
            self.assertEqual(len(set(session_ids)), 5)


if __name__ == "__main__":
    unittest.main()
