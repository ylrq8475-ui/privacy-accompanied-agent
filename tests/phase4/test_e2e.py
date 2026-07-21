from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from external_connector.weather import RealExternalConnector

from backend.app.adapters import PerceptionSource, VisionAdapter
from backend.app.api import DemoASGIApp
from backend.app.live import FALLBACK_TRANSCRIPT, LiveCoordinator
from backend.app.local_music import LocalMusicPlayer
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from tests.phase1c.helpers import FixedClock
from tests.phase2.helpers import json_request
from tests.phase3.helpers import FailingAudio, FailingStep3
from tests.phase4.helpers import FakeWeatherTransport, RecordingPlaybackBackend


class Phase4EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_phase4_live_chain_completes_five_out_of_five(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "demo.sqlite3"
            session_ids: list[str] = []
            for sample in range(1, 6):
                with self.subTest(sample=sample):
                    playback = RecordingPlaybackBackend()
                    orchestrator = Orchestrator(
                        clock=FixedClock(),
                        persistence=SQLitePersistence(database),
                        live_connector=RealExternalConnector(
                            transport=FakeWeatherTransport(), clock=FixedClock()
                        ),
                        live_music=LocalMusicPlayer(backend=playback),
                    )
                    live = LiveCoordinator(
                        orchestrator,
                        vision=VisionAdapter(
                            observers=[
                                (PerceptionSource.STATIC_SYNTHETIC, lambda: (True, None))
                            ]
                        ),
                        audio=FailingAudio(),
                        step3=FailingStep3(),
                        confirmation_samples=2,
                    )
                    app = DemoASGIApp(orchestrator, live=live)

                    await json_request(app, "POST", "/v1/live/perception/poll")
                    _, response = await json_request(
                        app, "POST", "/v1/live/perception/poll"
                    )
                    session = response["session"]
                    session_id = session["session_id"]
                    _, session = await json_request(
                        app,
                        "POST",
                        f"/v1/live/sessions/{session_id}/transcript",
                        {"text": FALLBACK_TRANSCRIPT},
                    )
                    _, session = await json_request(
                        app,
                        "POST",
                        f"/v1/live/sessions/{session_id}/clarifications",
                        {"answer": "PHYSICAL_FATIGUE"},
                    )
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
                    self.assertEqual(len(playback.paths), 1)
                    self.assertTrue(session["results"][music_id]["result"]["playback_started"])
                    self.assertEqual(
                        session["results"][ac_id]["result"]["message"],
                        "模拟执行成功",
                    )
                    events = orchestrator.audit_log.list_events(session_id)
                    self.assertTrue(any(e.event_type == "INTERNET_REQUEST" for e in events))
                    self.assertTrue(any(e.event_type == "LOCAL_ACTION_REQUEST" for e in events))
                    session_ids.append(session_id)

            self.assertEqual(len(session_ids), 5)
            self.assertEqual(len(set(session_ids)), 5)


if __name__ == "__main__":
    unittest.main()
