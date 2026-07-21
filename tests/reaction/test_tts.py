from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.app.adapters import (
    MAX_TTS_WAV_BYTES,
    AdapterError,
    StepAudioAdapter,
    WavTransportResponse,
)
from backend.app.api import DemoASGIApp
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.analysis import TextStateLabel, TextStateModelOutput
from backend.app.schemas.phase4 import WeatherSnapshot
from backend.app.schemas.reaction import LLMReaction
from tests.helpers import NOW


VALID_WAV = b"RIFF" + (4).to_bytes(4, "little") + b"WAVE"


class FakeWavTransport:
    def __init__(self, response: WavTransportResponse | AdapterError) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def request_wav(self, base_url, path, body, *, timeout_seconds, max_response_bytes):
        self.calls.append(
            {
                "base_url": base_url,
                "path": path,
                "body": body,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        if isinstance(self.response, AdapterError):
            raise self.response
        return self.response


class TTSAdapterTests(unittest.TestCase):
    def adapter(self, response: WavTransportResponse | AdapterError) -> StepAudioAdapter:
        return StepAudioAdapter(
            "http://stepaudio:8010",
            None,
            wav_transport=FakeWavTransport(response),
        )

    def test_valid_wav_is_returned_in_memory_with_bounded_local_request(self) -> None:
        transport = FakeWavTransport(WavTransportResponse(VALID_WAV, "audio/wav", 9))
        adapter = StepAudioAdapter(
            "http://stepaudio:8010", None, wav_transport=transport
        )
        body, latency = adapter.synthesize_wav("Synthetic response")
        self.assertEqual((body, latency), (VALID_WAV, 9))
        self.assertEqual(transport.calls[0]["path"], "/v1/audio/synthesize")
        self.assertEqual(transport.calls[0]["timeout_seconds"], 120.0)
        self.assertEqual(transport.calls[0]["max_response_bytes"], MAX_TTS_WAV_BYTES)

    def test_oversize_content_type_invalid_wav_and_timeout_are_explicit(self) -> None:
        cases = (
            (
                WavTransportResponse(
                    VALID_WAV + b"x" * MAX_TTS_WAV_BYTES, "audio/wav", 1
                ),
                "TTS_RESPONSE_TOO_LARGE",
            ),
            (WavTransportResponse(VALID_WAV, "application/json", 1), "TTS_CONTENT_TYPE_INVALID"),
            (WavTransportResponse(b"not-a-wave", "audio/wav", 1), "TTS_WAV_INVALID"),
            (AdapterError("TTS_UNAVAILABLE", "synthetic timeout"), "TTS_UNAVAILABLE"),
        )
        for response, code in cases:
            with self.subTest(code=code), self.assertRaises(AdapterError) as raised:
                self.adapter(response).synthesize_wav("Synthetic response")
            self.assertEqual(raised.exception.code, code)


async def raw_request(app: DemoASGIApp, path: str) -> tuple[int, dict[bytes, bytes], bytes]:
    sent: list[dict] = []
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(
        {"type": "http", "method": "POST", "path": path}, receive, send
    )
    start = next(item for item in sent if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


class FailingSynthesizer:
    def synthesize_wav(self, text: str):
        raise AdapterError("TTS_UNAVAILABLE", "synthetic timeout")


class ReadySynthesizer:
    def synthesize_wav(self, text: str):
        return VALID_WAV, 4


class TTSAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        persistence = SQLitePersistence(Path(self.temporary.name) / "tts.sqlite3")
        self.orchestrator = Orchestrator(persistence=persistence)
        output = TextStateModelOutput.model_validate(
            {"state_hypotheses": [{"label": "CALM", "confidence": 0.8, "evidence": []}]}
        )
        reaction = LLMReaction.model_validate(
            {
                "reply_text": "Synthetic text remains available.",
                "tone": "CALMING",
                "follow_up_question": None,
                "reasons": ["Synthetic context."],
                "suggestions": [],
            }
        )
        weather = WeatherSnapshot(
            city_code="310000",
            temperature_c=20,
            condition="clear",
            source="FIXED_DEMO",
            fetched_at=NOW,
            provider="FIXED_DEMO",
        )
        session = self.orchestrator.start_text_analysis_session(output, reaction, weather)
        self.orchestrator.confirm_text_state(
            session.session_id, TextStateLabel.CALM, reaction
        )
        self.session_id = session.session_id

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_success_proxies_audio_wav_without_persistence(self) -> None:
        app = DemoASGIApp(
            self.orchestrator, tts_synthesizer=ReadySynthesizer()  # type: ignore[arg-type]
        )
        status, headers, body = await raw_request(
            app, f"/v1/analysis/sessions/{self.session_id}/tts"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"content-type"], b"audio/wav")
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual(headers[b"x-model-latency-ms"], b"4")
        self.assertEqual(body, VALID_WAV)
        self.assertNotIn(VALID_WAV, self.orchestrator.persistence.path.read_bytes())

    async def test_failure_returns_text_only_json(self) -> None:
        app = DemoASGIApp(
            self.orchestrator, tts_synthesizer=FailingSynthesizer()  # type: ignore[arg-type]
        )
        status, headers, body = await raw_request(
            app, f"/v1/analysis/sessions/{self.session_id}/tts"
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers[b"content-type"].startswith(b"application/json"))
        self.assertEqual(
            json.loads(body), {"status": "TEXT_ONLY", "error": "TTS_UNAVAILABLE"}
        )
        snapshot = self.orchestrator.snapshot(self.session_id)
        self.assertEqual(snapshot["final_reaction"]["reply_text"], "Synthetic text remains available.")


if __name__ == "__main__":
    unittest.main()
