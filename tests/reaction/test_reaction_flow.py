from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.app.adapters import AdapterError
from backend.app.api import DemoASGIApp
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.policy import evaluate_reaction_suggestions
from backend.app.schemas.analysis import TextStateLabel, TextStateModelOutput
from backend.app.schemas.phase4 import WeatherSnapshot
from backend.app.schemas.reaction import LLMReaction
from external_connector.weather import RealExternalConnector
from tests.helpers import NOW
from tests.phase1c.test_api import http_request
from tests.phase4.helpers import FakeWeatherTransport


def weather_body(temperature: float) -> bytes:
    return json.dumps(
        {"current": {"temperature_2m": temperature, "weather_code": 0}}
    ).encode()


def reaction_data(suggestions: list[dict[str, str]], phase: str, label: str) -> dict[str, object]:
    return {
        "reply_text": f"Synthetic {phase.lower()} response for {label}.",
        "tone": "CELEBRATORY" if label == "HAPPY" else "SUPPORTIVE",
        "follow_up_question": None,
        "reasons": ["Synthetic structured context only."],
        "suggestions": suggestions,
    }


class RecordingAnalyzer:
    def __init__(self, suggestions: list[dict[str, str]] | None = None) -> None:
        self.suggestions = suggestions or []
        self.analysis_inputs: list[str] = []
        self.reaction_calls: list[dict[str, object]] = []
        self.fail_final = False

    def analyze_text_state(self, text: str) -> tuple[TextStateModelOutput, int]:
        self.analysis_inputs.append(text)
        return TextStateModelOutput.model_validate(
            {
                "state_hypotheses": [
                    {"label": "HAPPY", "confidence": 0.82, "evidence": ["synthetic"]},
                    {"label": "CALM", "confidence": 0.18, "evidence": []},
                ]
            }
        ), 11

    def generate_reaction(self, **context: object) -> tuple[LLMReaction, int]:
        self.reaction_calls.append(context)
        if self.fail_final and context["phase"] == "FINAL":
            raise AdapterError("MODEL_UNAVAILABLE", "synthetic final failure")
        label = context["label"]
        return LLMReaction.model_validate(
            reaction_data(self.suggestions, str(context["phase"]), label.value)
        ), 7


class ReactionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "flow.sqlite3"
        self.persistence = SQLitePersistence(self.database)
        connector = RealExternalConnector(
            transport=FakeWeatherTransport(weather_body(35.0)), clock=lambda: NOW
        )
        self.orchestrator = Orchestrator(
            persistence=self.persistence, live_connector=connector
        )
        self.analyzer = RecordingAnalyzer()
        self.app = DemoASGIApp(
            self.orchestrator, text_analyzer=self.analyzer  # type: ignore[arg-type]
        )

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def analyze_and_start(self, marker: str) -> tuple[dict, dict]:
        status, analysis = await http_request(
            self.app, "POST", "/v1/analysis/text", {"text": marker}
        )
        self.assertEqual(status, 200)
        status, session = await http_request(
            self.app,
            "POST",
            f"/v1/analysis/text/{analysis['analysis_id']}/sessions",
            {},
        )
        self.assertEqual(status, 201)
        return analysis, session

    async def test_all_nine_labels_regenerate_final_reactions_and_store_safe_summaries(self) -> None:
        raw_marker = "raw-private-text-marker-9385"
        labels = list(TextStateLabel)
        for index, label in enumerate(labels):
            analysis, session = await self.analyze_and_start(f"{raw_marker}-{index}")
            self.assertEqual(analysis["weather_context"]["source"], "REAL_API")
            status, completed = await http_request(
                self.app,
                "POST",
                f"/v1/analysis/sessions/{session['session_id']}/state-confirmation",
                {"label": label.value},
            )
            self.assertEqual(status, 200)
            self.assertEqual(completed["state"], "WAITING_AC_AUTHORIZATION")
            self.assertEqual(completed["confirmed_emotion"], label.value)
            self.assertIn(label.value, completed["final_reaction"]["reply_text"])
            self.assertEqual(completed["ac_decision"]["decision"], "COOL")
            self.assertIsNotNone(completed["ac_action"])
            decision_events = [
                event
                for event in self.orchestrator.audit_log.list_events(session["session_id"])
                if event.event_type == "AC_DECISION_COMPUTED"
            ]
            self.assertEqual(len(decision_events), 1)
            self.assertEqual(decision_events[0].payload["confirmed_emotion"], label.value)
            self.assertFalse(decision_events[0].payload["model_suggestion_used"])
            self.assertNotIn(raw_marker, json.dumps(decision_events[0].payload))
            status, completed = await http_request(
                self.app,
                "POST",
                f"/v1/analysis/sessions/{session['session_id']}/actions/{completed['ac_action']['action_id']}/authorization",
                {"approved": False},
            )
            self.assertEqual(status, 200)
            self.assertEqual(completed["state"], "COMPLETED")
            self.assertTrue(completed["summary_saved"])

        self.assertEqual(len(self.analyzer.analysis_inputs), 9)
        self.assertEqual(
            [call["phase"] for call in self.analyzer.reaction_calls].count("PROVISIONAL"), 9
        )
        self.assertEqual(
            [call["phase"] for call in self.analyzer.reaction_calls].count("FINAL"), 9
        )
        self.assertTrue(
            all(call["text"] is None for call in self.analyzer.reaction_calls if call["phase"] == "FINAL")
        )
        self.assertEqual(self.orchestrator.music.executed_action_ids, [])
        self.assertEqual(self.orchestrator.ac.executed_action_ids, [])
        self.assertEqual(len(self.persistence.list_emotion_summaries()), 9)
        database_bytes = self.database.read_bytes()
        self.assertNotIn(raw_marker.encode(), database_bytes)
        self.assertNotIn(b"Synthetic final response", database_bytes)
        self.assertNotIn(b"synthetic\"],\"label", database_bytes)

    async def test_final_model_failure_leaves_clarification_and_creates_no_action(self) -> None:
        _, session = await self.analyze_and_start("synthetic failure case")
        self.analyzer.fail_final = True
        status, body = await http_request(
            self.app,
            "POST",
            f"/v1/analysis/sessions/{session['session_id']}/state-confirmation",
            {"label": "ANXIOUS"},
        )
        self.assertEqual((status, body), (503, {"error": "MODEL_UNAVAILABLE"}))
        snapshot = self.orchestrator.snapshot(session["session_id"])
        self.assertEqual(snapshot["state"], "CLARIFICATION_REQUIRED")
        self.assertIsNone(snapshot["final_reaction"])
        self.assertIsNone(snapshot["music_action"])
        self.assertIsNone(snapshot["ac_action"])

    async def test_two_independent_actions_complete_five_out_of_five_without_physical_action(self) -> None:
        self.analyzer.suggestions = [
            {"type": "EMOTION_MATCHED_MUSIC"},
            {"type": "AC", "direction": "COOLER"},
        ]
        session_ids: set[str] = set()
        for index in range(5):
            _, session = await self.analyze_and_start(f"synthetic e2e {index}")
            _, session = await http_request(
                self.app,
                "POST",
                f"/v1/analysis/sessions/{session['session_id']}/state-confirmation",
                {"label": "HAPPY"},
            )
            music_id = session["music_action"]["action_id"]
            ac_id = session["ac_action"]["action_id"]
            self.assertNotEqual(music_id, ac_id)
            self.assertEqual(len(self.orchestrator.music.executed_action_ids), index)
            self.assertEqual(len(self.orchestrator.ac.executed_action_ids), index)
            _, session = await http_request(
                self.app,
                "POST",
                f"/v1/analysis/sessions/{session['session_id']}/actions/{music_id}/authorization",
                {"approved": True},
            )
            self.assertEqual(session["authorizations"][ac_id]["authorization_status"], "PENDING")
            _, session = await http_request(
                self.app,
                "POST",
                f"/v1/analysis/sessions/{session['session_id']}/actions/{ac_id}/authorization",
                {"approved": True},
            )
            self.assertEqual(session["state"], "COMPLETED")
            self.assertFalse(session["results"][ac_id]["result"]["physical_action_performed"])
            session_ids.add(session["session_id"])
        self.assertEqual(len(session_ids), 5)
        self.assertEqual(len(self.orchestrator.music.executed_action_ids), 5)
        self.assertEqual(len(self.orchestrator.ac.executed_action_ids), 5)

    async def test_one_music_suggestion_requires_authorization_and_completes_after_rejection(self) -> None:
        self.analyzer.suggestions = [
            {"type": "EMOTION_MATCHED_MUSIC"}
        ]
        _, session = await self.analyze_and_start("synthetic one suggestion")
        _, session = await http_request(
            self.app,
            "POST",
            f"/v1/analysis/sessions/{session['session_id']}/state-confirmation",
            {"label": "CALM"},
        )
        self.assertEqual(session["state"], "WAITING_MUSIC_AUTHORIZATION")
        self.assertIsNotNone(session["music_action"])
        self.assertIsNotNone(session["ac_action"])
        self.assertEqual(self.orchestrator.music.executed_action_ids, [])
        music_id = session["music_action"]["action_id"]
        _, session = await http_request(
            self.app,
            "POST",
            f"/v1/analysis/sessions/{session['session_id']}/actions/{music_id}/authorization",
            {"approved": False},
        )
        self.assertEqual(session["state"], "WAITING_AC_AUTHORIZATION")
        self.assertEqual(self.orchestrator.music.executed_action_ids, [])
        ac_id = session["ac_action"]["action_id"]
        _, session = await http_request(
            self.app,
            "POST",
            f"/v1/analysis/sessions/{session['session_id']}/actions/{ac_id}/authorization",
            {"approved": False},
        )
        self.assertEqual(session["state"], "COMPLETED")
        self.assertEqual(
            self.persistence.list_emotion_summaries()[0].music_outcome.value,
            "USER_REJECTED",
        )

    async def test_close_and_repeat_reset_store_exactly_one_summary(self) -> None:
        _, session = await self.analyze_and_start("synthetic close")
        for _ in range(2):
            status, session = await http_request(
                self.app,
                "POST",
                f"/v1/analysis/sessions/{session['session_id']}/reset",
                {},
            )
            self.assertEqual(status, 200)
        summaries = self.persistence.list_emotion_summaries()
        self.assertEqual(len(summaries), 1)
        self.assertFalse(summaries[0].user_confirmed)

    async def test_preference_and_summary_management_endpoints(self) -> None:
        status, preferences = await http_request(
            self.app, "GET", "/v1/user-preferences"
        )
        self.assertEqual(status, 200)
        self.assertFalse(preferences["reply_style"]["confirmed"])
        self.assertTrue(preferences["reply_style"]["runtime_default"])
        status, _ = await http_request(
            self.app,
            "POST",
            "/v1/user-preferences/confirm",
            {"key": "music_preference", "value": "NONE"},
        )
        self.assertEqual(status, 201)
        _, preferences = await http_request(self.app, "GET", "/v1/user-preferences")
        self.assertEqual(preferences["music_preference"]["value"], "NONE")
        self.assertTrue(preferences["music_preference"]["confirmed"])
        status, _ = await http_request(
            self.app, "DELETE", "/v1/user-preferences/music_preference"
        )
        self.assertEqual(status, 200)

        _, session = await self.analyze_and_start("synthetic summary API")
        await http_request(
            self.app,
            "POST",
            f"/v1/analysis/sessions/{session['session_id']}/reset",
            {},
        )
        status, body = await http_request(self.app, "GET", "/v1/emotion-summaries")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["summaries"]), 1)
        status, body = await http_request(
            self.app, "DELETE", "/v1/emotion-summaries"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted_count"], 1)


class ReactionPolicyTests(unittest.TestCase):
    def reaction(self, suggestions: list[dict[str, str]]) -> LLMReaction:
        return LLMReaction.model_validate(reaction_data(suggestions, "FINAL", "CALM"))

    def weather(self, temperature: float, source: str = "REAL_API") -> WeatherSnapshot:
        return WeatherSnapshot(
            city_code="310000",
            temperature_c=temperature,
            condition="clear",
            source=source,
            fetched_at=NOW,
            provider="OPEN_METEO" if source != "FIXED_DEMO" else "FIXED_DEMO",
        )

    def test_music_preference_and_deterministic_ac_rules(self) -> None:
        music = self.reaction([{"type": "EMOTION_MATCHED_MUSIC"}])
        rejected_music = evaluate_reaction_suggestions(
            music,
            self.weather(20),
            music_preference="NONE",
            music_preference_confirmed=True,
            confirmed_emotion="STRESSED",
        )
        self.assertIsNone(rejected_music.music_track_id)
        self.assertEqual(rejected_music.decisions[0].reason_code, "MUSIC_PREFERENCE_NONE")

        cases = (
            # Direction is deliberately ignored; weather and emotion own the decision.
            (10, "COOLER", "REAL_API", "CALM", "HEAT", "heat", 23, 0),
            (31, "WARMER", "CACHE", "ANXIOUS", "COOL", "cool", 25, -1),
            (31, "COOLER", "REAL_API", "LONELY", "COOL", "cool", 27, 1),
            (20, "WARMER", "REAL_API", "STRESSED", "OFF", None, None, 0),
            (31, "COOLER", "FIXED_DEMO", "CALM", "UNAVAILABLE", None, None, 0),
        )
        for temperature, direction, source, emotion, decision, mode, target, offset in cases:
            with self.subTest(temperature=temperature, direction=direction, source=source):
                result = evaluate_reaction_suggestions(
                    self.reaction([{"type": "AC", "direction": direction}]),
                    self.weather(temperature, source),
                    music_preference=None,
                    music_preference_confirmed=False,
                    confirmed_emotion=emotion,
                )
                self.assertEqual(result.ac_decision.decision.value, decision)
                self.assertEqual(result.ac_decision.emotion_adjustment_c, offset)
                if mode is not None:
                    self.assertEqual(result.ac_payload.mode.value, mode)
                    self.assertEqual(result.ac_payload.target_temperature, target)
                else:
                    self.assertIsNone(result.ac_payload)
                    self.assertIsNone(result.ac_decision.base_target_temperature)
                    self.assertIsNone(result.ac_decision.target_temperature)

    def test_ac_is_decided_without_llm_suggestion_and_uses_half_up_boundaries(self) -> None:
        cases = (
            (14.9, "PHYSICAL_FATIGUE", "HEAT", 23),
            (15, "PHYSICAL_FATIGUE", "OFF", None),
            (26, "ANXIOUS", "OFF", None),
            (26.1, "ANXIOUS", "COOL", 26),
            # 29C gives a 26.5C base; ROUND_HALF_UP must produce 27C.
            (29, "CALM", "COOL", 27),
        )
        for temperature, emotion, decision, target in cases:
            with self.subTest(temperature=temperature, emotion=emotion):
                result = evaluate_reaction_suggestions(
                    self.reaction([]),
                    self.weather(temperature),
                    music_preference=None,
                    music_preference_confirmed=False,
                    confirmed_emotion=emotion,
                )
                self.assertEqual(result.ac_decision.decision.value, decision)
                self.assertEqual(result.ac_decision.target_temperature, target)
                self.assertEqual(result.ac_payload is not None, target is not None)

    def test_all_nine_emotion_adjustments_are_applied_after_base_temperature(self) -> None:
        expected = {
            "PHYSICAL_FATIGUE": (1, 28),
            "EMOTIONAL_LOW": (1, 28),
            "LONELY": (1, 28),
            "STRESSED": (-1, 26),
            "ANXIOUS": (-1, 26),
            "ANGRY": (-1, 26),
            "HAPPY": (0, 27),
            "CALM": (0, 27),
            "OTHER": (0, 27),
        }
        for emotion, (offset, target) in expected.items():
            with self.subTest(emotion=emotion):
                result = evaluate_reaction_suggestions(
                    self.reaction([]),
                    self.weather(29),
                    music_preference=None,
                    music_preference_confirmed=False,
                    confirmed_emotion=emotion,
                )
                self.assertEqual(result.ac_decision.base_target_temperature, 26.5)
                self.assertEqual(result.ac_decision.emotion_adjustment_c, offset)
                self.assertEqual(result.ac_decision.target_temperature, target)


if __name__ == "__main__":
    unittest.main()
