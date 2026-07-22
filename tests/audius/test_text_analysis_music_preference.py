from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.local_music import LocalMusicPlayer
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.analysis import (
    TextStateLabel,
    TextStateModelOutput,
)
from backend.app.schemas.persistence import (
    UserPreferenceConfirmation,
)
from backend.app.schemas.phase4 import WeatherSnapshot
from backend.app.schemas.reaction import LLMReaction
from tests.phase1c.helpers import FixedClock, NOW
from tests.phase4.helpers import make_synthetic_music_root


class TextAnalysisMusicPreferenceTests(unittest.TestCase):
    @staticmethod
    def _analysis() -> TextStateModelOutput:
        return TextStateModelOutput.model_validate(
            {
                "state_hypotheses": [
                    {
                        "label": "PHYSICAL_FATIGUE",
                        "confidence": 0.95,
                        "evidence": ["??????????"],
                    }
                ]
            }
        )

    @staticmethod
    def _reaction() -> LLMReaction:
        return LLMReaction.model_validate(
            {
                "reply_text": "??????????????",
                "tone": "GROUNDING",
                "follow_up_question": None,
                "reasons": ["??????????"],
                "suggestions": [],
            }
        )

    @staticmethod
    def _weather() -> WeatherSnapshot:
        return WeatherSnapshot.model_validate(
            {
                "city_code": "310000",
                "temperature_c": 33.0,
                "condition": "partly_cloudy",
                "source": "REAL_API",
                "fetched_at": NOW,
                "provider": "OPEN_METEO",
            }
        )

    def test_confirmed_preference_creates_browser_music_delivery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            music_root = make_synthetic_music_root(root)
            player = LocalMusicPlayer(
                music_root,
                delivery_mode="BROWSER",
            )

            orchestrator = Orchestrator(
                clock=FixedClock(),
                persistence=SQLitePersistence(
                    root / "demo.sqlite3"
                ),
                live_music=player,
            )
            orchestrator.initialize_persistence()

            orchestrator.confirm_user_preference(
                UserPreferenceConfirmation(
                    key="music_preference",
                    value="EMOTION_MATCHED",
                )
            )

            session = orchestrator.start_text_analysis_session(
                self._analysis(),
                self._reaction(),
                self._weather(),
            )

            session = orchestrator.confirm_text_state(
                session.session_id,
                TextStateLabel.PHYSICAL_FATIGUE,
                self._reaction(),
            )

            self.assertIsNotNone(session.music_action)
            assert session.music_action is not None

            action_id = session.music_action.action_id

            accepted_music = [
                decision
                for decision in session.policy_suggestion_decisions
                if decision.suggestion_type
                == "EMOTION_MATCHED_MUSIC"
            ]

            self.assertEqual(len(accepted_music), 1)
            self.assertTrue(accepted_music[0].accepted)
            self.assertEqual(
                accepted_music[0].reason_code,
                "MUSIC_PREFERENCE_EMOTION_MATCHED",
            )

            session = orchestrator.authorize(
                session.session_id,
                action_id,
                True,
            )

            result = session.results[action_id].result

            self.assertTrue(result["delivery_ready"])
            self.assertFalse(result["playback_started"])
            self.assertEqual(
                result["playback_scope"],
                "BROWSER_LOCAL",
            )

            delivery = player.consume_browser_delivery(action_id)
            self.assertIsNotNone(delivery)

            player.close()


if __name__ == "__main__":
    unittest.main()
