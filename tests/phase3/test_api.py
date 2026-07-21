from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path

from backend.app.adapters import AdapterError, AdapterHealth, PerceptionSource, VisionAdapter
from backend.app.api import DemoASGIApp
from backend.app.live import LiveCoordinator
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.visual import VisualPerceptionOutput
from tests.phase1c.helpers import FixedClock
from tests.phase2.helpers import json_request, request
from tests.phase3.helpers import FailingAudio, FailingStep3


class HealthAudio(FailingAudio):
    def health(self):
        return AdapterHealth("STEPAUDIO", False, "MODEL_UNAVAILABLE", 3)


class HealthStep3(FailingStep3):
    def health(self):
        return AdapterHealth("STEP3", False, "MODEL_UNAVAILABLE", 4)


class SequencedVisualStep3(HealthStep3):
    def __init__(self, *results: dict[str, object] | AdapterError) -> None:
        super().__init__()
        self.results = deque(results)
        self.visual_calls = 0

    def perceive_image(self, image: bytes, media_type: str):
        self.visual_calls += 1
        self.asserted_png = image.startswith(b"\x89PNG\r\n\x1a\n")
        self.asserted_media_type = media_type
        result = self.results.popleft()
        if isinstance(result, AdapterError):
            raise result
        return VisualPerceptionOutput.model_validate(result), 27


def visual_result(person_present: bool) -> dict[str, object]:
    return {
        "person_present": person_present,
        "scene_type": "INDOOR",
        "scene_summary": (
            "合成客厅中有一名人物。"
            if person_present
            else "合成客厅中没有人物。"
        ),
        "confidence": 0.94,
        "evidence": ["合成场景证据"],
    }


class Phase3APIContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "demo.sqlite3"
        self.orchestrator = Orchestrator(
            clock=FixedClock(),
            persistence=SQLitePersistence(self.database),
        )
        self.live = LiveCoordinator(
            self.orchestrator,
            vision=VisionAdapter(
                observers=[
                    (PerceptionSource.STATIC_SYNTHETIC, lambda: (True, None))
                ]
            ),
            audio=HealthAudio(),
            step3=HealthStep3(),
            confirmation_samples=2,
        )
        self.app = DemoASGIApp(self.orchestrator, live=self.live)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_health_discloses_status_but_not_internal_urls_or_paths(self) -> None:
        status, body = await json_request(self.app, "GET", "/v1/live/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "LIVE_WITH_SAFE_FALLBACKS")
        self.assertFalse(body["raw_media_persisted"])
        serialized = str(body)
        self.assertNotIn("stepaudio:8010", serialized)
        self.assertNotIn("step3-vl:8000", serialized)
        audius = next(
            item for item in body["components"] if item["component"] == "AUDIUS_MUSIC"
        )
        self.assertEqual(audius["status"], "NOT_CONFIGURED")

    async def test_live_requests_reject_extra_or_free_text(self) -> None:
        status, response = await json_request(
            self.app, "POST", "/v1/live/perception/poll", {"path": "private.wav"}
        )
        self.assertEqual(status, 422)
        await json_request(self.app, "POST", "/v1/live/perception/poll")
        _, result = await json_request(self.app, "POST", "/v1/live/perception/poll")
        session_id = result["session"]["session_id"]
        status, response = await json_request(
            self.app,
            "POST",
            f"/v1/live/sessions/{session_id}/transcript",
            {"text": "任意真实用户文本"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(response["error"], "VALIDATION_ERROR")

    async def test_tts_failure_returns_text_only_and_never_claims_audio(self) -> None:
        await json_request(self.app, "POST", "/v1/live/perception/poll")
        _, result = await json_request(self.app, "POST", "/v1/live/perception/poll")
        session_id = result["session"]["session_id"]
        await json_request(
            self.app,
            "POST",
            f"/v1/live/sessions/{session_id}/transcript",
            {"text": "今天有点累"},
        )
        status, tts = await json_request(
            self.app, "POST", f"/v1/live/sessions/{session_id}/tts"
        )
        self.assertEqual(status, 200)
        self.assertEqual(tts["status"], "TEXT_ONLY")
        self.assertIsNone(tts["audio_ref"])

    async def test_synthetic_scene_manifest_and_person_result_start_text_only_session(self) -> None:
        step3 = SequencedVisualStep3(visual_result(True), visual_result(True))
        self.live.step3 = step3

        status, manifest = await json_request(
            self.app, "GET", "/v1/live/perception/scenes"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["scene_id"] for item in manifest["scenes"]],
            ["indoor_person", "indoor_empty"],
        )
        self.assertTrue(all(item["synthetic"] for item in manifest["scenes"]))
        image_status, headers, image = await request(
            self.app,
            "GET",
            "/v1/live/perception/scenes/indoor_person/image",
        )
        self.assertEqual(image_status, 200)
        self.assertEqual(headers[b"content-type"], b"image/png")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLessEqual(len(image), 524_288)

        status, result = await json_request(
            self.app,
            "POST",
            "/v1/live/perception/analyze",
            {"scene_id": "indoor_person"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(step3.asserted_png)
        self.assertTrue(result["observation"]["person_present"])
        self.assertEqual(result["observation"]["network_scope"], "LOCAL")
        session = result["session"]
        self.assertEqual(session["state"], "LISTENING")
        self.assertEqual(session["perception_source"], "SYNTHETIC_IMAGE")
        self.assertEqual(session["interaction_source"], "TEXT_FALLBACK_PENDING")
        self.assertEqual(session["model_source"], "NOT_CALLED")
        self.assertEqual(session["visual_perception"]["scene_type"], "INDOOR")
        self.assertIsNone(session["music_action"])
        self.assertIsNone(session["ac_action"])

        _, repeated = await json_request(
            self.app,
            "POST",
            "/v1/live/perception/analyze",
            {"scene_id": "indoor_person"},
        )
        self.assertEqual(repeated["session"]["session_id"], session["session_id"])
        self.assertEqual(len(self.orchestrator.sessions), 1)

        events = self.orchestrator.audit_log.list_events(session["session_id"])
        visual_event = next(event for event in events if event.event_type == "STEP3_VISION_CALL")
        self.assertEqual(visual_event.network_scope.value, "LOCAL")
        self.assertEqual(visual_event.latency_ms, 27)
        serialized = str([event.model_dump(mode="json") for event in events])
        self.assertNotIn("data:image", serialized)
        self.assertNotIn("iVBOR", serialized)
        raw_database = self.database.read_bytes()
        self.assertNotIn(b"data:image", raw_database)
        self.assertNotIn(b"iVBOR", raw_database)
        self.assertNotIn(b"\x89PNG\r\n\x1a\n", raw_database)

    async def test_empty_scene_does_not_create_session_and_ends_existing_visual_session(self) -> None:
        step3 = SequencedVisualStep3(
            visual_result(False), visual_result(True), visual_result(False)
        )
        self.live.step3 = step3

        _, empty = await json_request(
            self.app,
            "POST",
            "/v1/live/perception/analyze",
            {"scene_id": "indoor_empty"},
        )
        self.assertIsNone(empty["session"])
        self.assertEqual(len(self.orchestrator.sessions), 0)

        _, appeared = await json_request(
            self.app,
            "POST",
            "/v1/live/perception/analyze",
            {"scene_id": "indoor_person"},
        )
        session_id = appeared["session"]["session_id"]
        _, left = await json_request(
            self.app,
            "POST",
            "/v1/live/perception/analyze",
            {"scene_id": "indoor_empty"},
        )
        self.assertEqual(left["session"]["session_id"], session_id)
        self.assertFalse(left["session"]["active"])
        self.assertFalse(left["session"]["person_present"])
        self.assertEqual(len(self.orchestrator.sessions), 1)

    async def test_visual_failure_and_invalid_scene_leave_session_unchanged(self) -> None:
        step3 = SequencedVisualStep3(
            AdapterError("MODEL_UNAVAILABLE", "synthetic model failure")
        )
        self.live.step3 = step3
        status, failure = await json_request(
            self.app,
            "POST",
            "/v1/live/perception/analyze",
            {"scene_id": "indoor_person"},
        )
        self.assertEqual(status, 503)
        self.assertEqual(failure["error"], "MODEL_UNAVAILABLE")
        self.assertTrue(failure["session_unchanged"])
        self.assertRegex(failure["attempt_id"], r"^perception-[a-f0-9]{32}$")
        self.assertEqual(len(self.orchestrator.sessions), 0)

        status, invalid = await json_request(
            self.app,
            "POST",
            "/v1/live/perception/analyze",
            {"scene_id": "../../private"},
        )
        self.assertEqual(status, 422)
        self.assertEqual(invalid["error"], "VALIDATION_ERROR")


if __name__ == "__main__":
    unittest.main()
