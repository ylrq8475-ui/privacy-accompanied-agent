from __future__ import annotations

import json
import unittest

from backend.app.adapters import (
    AdapterError,
    LocalJSONTransport,
    Step3Adapter,
    StepAudioAdapter,
    TransportResponse,
)
from tests.helpers import step3_data
from tests.phase3.helpers import FakeTransport


class StepAudioAdapterTests(unittest.TestCase):
    def test_asr_accepts_one_strict_transcript_and_records_no_media(self) -> None:
        transport = FakeTransport(TransportResponse(200, {"transcript": "今天有点累"}, 17))
        result = StepAudioAdapter(
            "http://stepaudio:8010", "synthetic-demo.wav", transport=transport
        ).transcribe()
        self.assertEqual(result.text, "今天有点累")
        self.assertEqual(result.latency_ms, 17)
        self.assertEqual(transport.requests[0]["body"], {"filename": "synthetic-demo.wav"})

    def test_asr_rejects_unsafe_filename_and_ambiguous_or_extra_response(self) -> None:
        with self.assertRaisesRegex(AdapterError, "safe shared synthetic"):
            StepAudioAdapter("http://stepaudio:8010", "../private.wav").transcribe()
        invalid = (
            {"text": "one", "transcript": "two"},
            {"transcript": "ok", "raw_audio": "forbidden"},
            {"transcript": 4},
        )
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(AdapterError):
                adapter = StepAudioAdapter(
                    "http://stepaudio:8010",
                    "synthetic.wav",
                    transport=FakeTransport(TransportResponse(200, body, 1)),
                )
                adapter.transcribe()

    def test_tts_rejects_urls_and_absolute_paths(self) -> None:
        for reference in ("https://example.test/audio.wav", "C:/private/audio.wav"):
            with self.subTest(reference=reference), self.assertRaises(AdapterError):
                adapter = StepAudioAdapter(
                    "http://stepaudio:8010",
                    "synthetic.wav",
                    transport=FakeTransport(
                        TransportResponse(200, {"filename": reference}, 2)
                    ),
                )
                adapter.synthesize("合成演示文本")


class Step3AdapterTests(unittest.TestCase):
    def _response(self, value: object) -> TransportResponse:
        return TransportResponse(
            200,
            {"choices": [{"message": {"content": json.dumps(value)}}]},
            23,
        )

    def test_valid_structured_output(self) -> None:
        transport = FakeTransport(self._response(step3_data()))
        output, latency = Step3Adapter(
            "http://step3-vl:8000", transport=transport
        ).analyze("今天有点累", b"synthetic-jpeg")
        self.assertEqual(latency, 23)
        self.assertEqual(output.state_hypotheses[0].label.value, "PHYSICAL_FATIGUE")
        request_text = json.dumps(transport.requests[0]["body"])
        self.assertNotIn("synthetic-jpeg", request_text)
        request_body = transport.requests[0]["body"]
        self.assertEqual(request_body["max_tokens"], 768)
        self.assertEqual(request_body["tool_choice"], "none")
        self.assertEqual(
            request_body["chat_template_kwargs"], {"enable_thinking": False}
        )
        self.assertEqual(request_body["thinking_token_budget"], 0)
        self.assertEqual(request_body["response_format"]["type"], "json_schema")
        self.assertTrue(request_body["response_format"]["json_schema"]["strict"])

    def test_visual_perception_uses_one_bounded_png_and_strict_schema(self) -> None:
        transport = FakeTransport(
            self._response(
                {
                    "person_present": True,
                    "scene_type": "INDOOR",
                    "scene_summary": "合成客厅中站着一个人。",
                    "confidence": 0.91,
                    "evidence": ["画面中央有完整人形", "背景包含沙发和落地灯"],
                }
            )
        )
        image = b"\x89PNG\r\n\x1a\nsynthetic-only"
        output, latency = Step3Adapter(
            "http://step3-vl:8000", transport=transport
        ).perceive_png(image)

        self.assertTrue(output.person_present)
        self.assertEqual(output.scene_type.value, "INDOOR")
        self.assertEqual(latency, 23)
        request = transport.requests[0]
        body = request["body"]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["timeout_seconds"], 30.0)
        self.assertEqual(body["max_tokens"], 384)
        self.assertEqual(body["tool_choice"], "none")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        image_url = body["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertNotIn("synthetic-only", json.dumps(body))
        prompt = body["messages"][0]["content"][0]["text"]
        self.assertIn("scene_summary 和 evidence 必须使用简体中文", prompt)

    def test_visual_perception_rejects_invalid_media_and_unsafe_output(self) -> None:
        with self.assertRaisesRegex(AdapterError, "approved image"):
            Step3Adapter("http://step3-vl:8000").perceive_png(b"not-png")
        with self.assertRaisesRegex(AdapterError, "512 KiB"):
            Step3Adapter("http://step3-vl:8000").perceive_png(
                b"\x89PNG\r\n\x1a\n" + b"x" * 524_288
            )

        unsafe = {
            "person_present": True,
            "scene_type": "INDOOR",
            "scene_summary": "synthetic",
            "confidence": 0.9,
            "evidence": [],
            "execute": True,
        }
        with self.assertRaisesRegex(AdapterError, "forbidden Step3 field"):
            Step3Adapter(
                "http://step3-vl:8000",
                transport=FakeTransport(self._response(unsafe)),
            ).perceive_png(b"\x89PNG\r\n\x1a\nsynthetic")

        invalid_json = TransportResponse(
            200,
            {"choices": [{"message": {"content": "not-json"}}]},
            3,
        )
        with self.assertRaisesRegex(AdapterError, "was not JSON"):
            Step3Adapter(
                "http://step3-vl:8000", transport=FakeTransport(invalid_json)
            ).perceive_png(b"\x89PNG\r\n\x1a\nsynthetic")

    def test_visual_perception_accepts_bounded_demo_jpeg(self) -> None:
        transport = FakeTransport(
            self._response(
                {
                    "person_present": False,
                    "scene_type": "INDOOR",
                    "scene_summary": "演示照片中没有人物。",
                    "confidence": 0.93,
                    "evidence": [],
                }
            )
        )
        image = b"\xff\xd8synthetic-jpeg\xff\xd9"
        output, latency = Step3Adapter(
            "http://step3-vl:8000", transport=transport
        ).perceive_image(image, "image/jpeg")

        self.assertFalse(output.person_present)
        self.assertEqual(latency, 23)
        image_url = transport.requests[0]["body"]["messages"][0]["content"][1][
            "image_url"
        ]["url"]
        self.assertTrue(image_url.startswith("data:image/jpeg;base64,"))
        self.assertNotIn("synthetic-jpeg", json.dumps(transport.requests[0]["body"]))

    def test_health_uses_model_discovery_and_requires_the_served_model(self) -> None:
        ready_transport = FakeTransport(
            TransportResponse(
                200,
                {"object": "list", "data": [{"id": "step3-vl"}]},
                7,
            )
        )
        health = Step3Adapter(
            "http://step3-vl:8000", transport=ready_transport
        ).health()
        self.assertTrue(health.available)
        self.assertEqual(health.status, "READY")
        self.assertEqual(ready_transport.requests[0]["path"], "/v1/models")

        unavailable = Step3Adapter(
            "http://step3-vl:8000",
            transport=FakeTransport(
                TransportResponse(200, {"object": "list", "data": [],}, 5)
            ),
        ).health()
        self.assertFalse(unavailable.available)
        self.assertEqual(unavailable.status, "MODEL_NOT_READY")

    def test_tool_calls_forbidden_fields_and_schema_errors_are_rejected(self) -> None:
        tool_call = TransportResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": "{}",
                            "tool_calls": [
                                {
                                    "id": "call-forbidden",
                                    "type": "function",
                                    "function": {
                                        "name": "forbidden",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            1,
        )
        forbidden = step3_data()
        forbidden["recommended_action"]["execute"] = True
        invalid_schema = step3_data()
        invalid_schema["state_hypotheses"][0]["confidence"] = 2
        for response in (tool_call, self._response(forbidden), self._response(invalid_schema)):
            with self.subTest(response=response.body), self.assertRaises(AdapterError):
                Step3Adapter(
                    "http://step3-vl:8000", transport=FakeTransport(response)
                ).analyze("synthetic")

    def test_empty_tool_call_envelope_is_not_an_actual_tool_call(self) -> None:
        response = self._response(step3_data())
        response.body["choices"][0]["message"]["tool_calls"] = []
        output, _ = Step3Adapter(
            "http://step3-vl:8000", transport=FakeTransport(response)
        ).analyze("synthetic")
        self.assertEqual(output.state_hypotheses[0].label.value, "PHYSICAL_FATIGUE")

    def test_local_transport_rejects_public_or_https_destinations_before_io(self) -> None:
        transport = LocalJSONTransport()
        for url in ("https://step3-vl:8000", "http://public.example:8000"):
            with self.subTest(url=url), self.assertRaisesRegex(
                AdapterError, "allowlisted LOCAL"
            ):
                transport.request(
                    "GET", url, "/health", None,
                    timeout_seconds=0.01, max_response_bytes=100,
                )


if __name__ == "__main__":
    unittest.main()
