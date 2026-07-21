from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from backend.app.adapters import AdapterError, Step3Adapter, TransportResponse
from backend.app.schemas.analysis import TextStateLabel, TextStateModelOutput
from backend.app.schemas.persistence import EmotionHistoryContext
from backend.app.schemas.phase4 import WeatherSnapshot
from backend.app.schemas.reaction import LLMReaction
from tests.phase3.helpers import FakeTransport
from tests.helpers import NOW


def valid_reaction() -> dict[str, object]:
    return {
        "reply_text": "That sounds like a meaningful moment.",
        "tone": "SUPPORTIVE",
        "follow_up_question": "Would a quiet pause help?",
        "reasons": ["The confirmed state is emotionally significant."],
        "suggestions": [
            {"type": "EMOTION_MATCHED_MUSIC"},
            {"type": "AC", "direction": "COOLER"},
        ],
    }


def response(content: str, *, tool_calls: object = None) -> TransportResponse:
    message: dict[str, object] = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return TransportResponse(200, {"choices": [{"message": message}]}, 13)


class ReactionSchemaTests(unittest.TestCase):
    def test_valid_reaction_has_advice_only(self) -> None:
        reaction = LLMReaction.model_validate(valid_reaction())
        self.assertEqual(reaction.suggestions[0].type, "EMOTION_MATCHED_MUSIC")
        self.assertNotIn("authorization", reaction.model_dump_json())

    def test_invalid_category_lengths_duplicates_and_command_fields_are_rejected(self) -> None:
        cases: list[dict[str, object]] = []
        for key, value in (
            ("tone", "DIAGNOSTIC"),
            ("reply_text", "x" * 201),
            ("follow_up_question", "x" * 121),
        ):
            candidate = valid_reaction()
            candidate[key] = value
            cases.append(candidate)
        duplicate = valid_reaction()
        duplicate["suggestions"] = [
            {"type": "AC", "direction": "COOLER"},
            {"type": "AC", "direction": "WARMER"},
        ]
        cases.append(duplicate)
        for forbidden in ("authorization_status", "execute", "skip_confirmation", "write_memory", "tool_call"):
            candidate = valid_reaction()
            candidate[forbidden] = True
            cases.append(candidate)
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError):
                LLMReaction.model_validate(candidate)


class ReactionAdapterTests(unittest.TestCase):
    def context(self) -> dict[str, object]:
        return {
            "label": TextStateLabel.HAPPY,
            "candidates": TextStateModelOutput.model_validate(
                {"state_hypotheses": [{"label": "HAPPY", "confidence": 0.9, "evidence": []}]}
            ),
            "weather": WeatherSnapshot(
                city_code="310000",
                temperature_c=31,
                condition="clear",
                source="REAL_API",
                fetched_at=NOW,
                provider="OPEN_METEO",
            ),
            "reply_style": "GENTLE",
            "reply_style_confirmed": False,
            "music_preference": None,
            "music_preference_confirmed": False,
            "history": EmotionHistoryContext(state_counts={}, recent_states=[]),
            "phase": "PROVISIONAL",
            "text": "synthetic text",
        }

    def test_reaction_uses_second_local_step3_call_contract(self) -> None:
        transport = FakeTransport(response(json.dumps(valid_reaction())))
        reaction, latency = Step3Adapter(
            "http://step3-vl:8000", transport=transport
        ).generate_reaction(**self.context())
        request = transport.requests[0]
        body = request["body"]
        self.assertEqual(reaction.tone.value, "SUPPORTIVE")
        self.assertEqual(latency, 13)
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(body["tool_choice"], "none")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertNotIn("tools", body)
        prompt = body["messages"][0]["content"]
        self.assertIn("Simplified Chinese", prompt)
        self.assertIn("reply_text", prompt)
        self.assertIn("follow_up_question", prompt)
        self.assertIn("reasons", prompt)
        self.assertIn("enum literals", prompt)

    def test_prose_fences_tool_calls_forbidden_and_invalid_schema_are_rejected(self) -> None:
        encoded = json.dumps(valid_reaction())
        forbidden = valid_reaction()
        forbidden["execute"] = True
        cases = (
            (response(f"Result: {encoded}"), "STEP3_REACTION_INVALID"),
            (response(f"```json\n{encoded}\n```"), "STEP3_REACTION_INVALID"),
            (response(encoded, tool_calls=[{"id": "unsafe"}]), "STEP3_TOOL_CALL_REJECTED"),
            (response(json.dumps(forbidden)), "STEP3_FORBIDDEN_FIELD"),
            (response(json.dumps({"reply_text": "missing fields"})), "STEP3_REACTION_INVALID"),
        )
        for upstream, code in cases:
            with self.subTest(code=code), self.assertRaises(AdapterError) as raised:
                Step3Adapter(
                    "http://step3-vl:8000", transport=FakeTransport(upstream)
                ).generate_reaction(**self.context())
            self.assertEqual(raised.exception.code, code)


if __name__ == "__main__":
    unittest.main()
