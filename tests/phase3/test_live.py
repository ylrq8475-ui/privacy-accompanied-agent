from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.adapters import PerceptionSource, VisionAdapter
from backend.app.live import LiveCoordinator
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.state_machine import DemoState
from tests.phase1c.helpers import FixedClock
from tests.phase3.helpers import FailingAudio, FailingStep3


class LiveCoordinatorTests(unittest.TestCase):
    def _coordinator(self, sequence: list[bool], database: Path):
        values = iter(sequence)
        vision = VisionAdapter(
            observers=[
                (PerceptionSource.STATIC_SYNTHETIC, lambda: (next(values), None))
            ]
        )
        step3 = FailingStep3()
        orchestrator = Orchestrator(
            clock=FixedClock(), persistence=SQLitePersistence(database)
        )
        coordinator = LiveCoordinator(
            orchestrator,
            vision=vision,
            audio=FailingAudio(),
            step3=step3,
            confirmation_samples=2,
        )
        return orchestrator, coordinator, step3

    def test_person_edges_are_debounced_and_heavy_model_runs_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            orchestrator, live, step3 = self._coordinator(
                [True, True, True, False, False], Path(temporary) / "demo.sqlite3"
            )
            _, session = live.poll()
            self.assertIsNone(session)
            _, session = live.poll()
            self.assertEqual(session.state, DemoState.LISTENING)
            self.assertEqual(session.interaction_source, "TEXT_FALLBACK_PENDING")
            live.submit_fallback_transcript(session.session_id, "今天有点累")
            self.assertEqual(step3.calls, 1)
            orchestrator.clarify(session.session_id, session.step3_output.state_hypotheses[0].label)
            self.assertEqual(session.state, DemoState.WAITING_MUSIC_AUTHORIZATION)
            live.poll()
            self.assertEqual(step3.calls, 1)
            live.poll()
            _, left_session = live.poll()
            self.assertIsNone(left_session)
            self.assertFalse(session.active)
            self.assertEqual(orchestrator.music.executed_action_ids, [])
            self.assertEqual(orchestrator.ac.executed_action_ids, [])
            event_types = [
                event.event_type
                for event in orchestrator.audit_log.list_events(session.session_id)
            ]
            self.assertEqual(event_types.count("VISION_OBSERVATION"), 2)
            self.assertEqual(event_types.count("STEP3_MODEL_CALL"), 1)

    def test_raw_media_and_full_model_payload_are_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "demo.sqlite3"
            _, live, _ = self._coordinator([True, True], database)
            live.poll()
            _, session = live.poll()
            live.submit_fallback_transcript(session.session_id, "今天有点累")
            raw_database = database.read_bytes()
            for forbidden in ("今天有点累", "raw_audio", "raw_video", "data:image"):
                self.assertNotIn(forbidden.encode("utf-8"), raw_database)


if __name__ == "__main__":
    unittest.main()
