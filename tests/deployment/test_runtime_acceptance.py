from __future__ import annotations

import unittest

from scripts.dgx_runtime_acceptance import verify_ac_decision


class RuntimeAcceptanceDecisionTests(unittest.TestCase):
    def verify(
        self,
        temperature: float,
        emotion: str,
        decision: str,
        target: int | None,
        adjustment: int,
        *,
        source: str = "REAL_API",
    ) -> None:
        result = verify_ac_decision(
            {"source": source, "temperature_c": temperature},
            emotion,
            {
                "decision": decision,
                "target_temperature": target,
                "emotion_adjustment_c": adjustment,
            },
        )
        self.assertTrue(result["verified"], result)

    def test_heating_formula_and_cooler_emotion(self) -> None:
        self.verify(10.0, "ANXIOUS", "HEAT", 22, -1)

    def test_comfort_boundaries_are_off(self) -> None:
        self.verify(15.0, "LONELY", "OFF", None, 0)
        self.verify(26.0, "STRESSED", "OFF", None, 0)

    def test_cooling_formula_and_warmer_emotion(self) -> None:
        self.verify(32.0, "EMOTIONAL_LOW", "COOL", 27, 1, source="CACHE")

    def test_fixed_demo_weather_is_unavailable(self) -> None:
        self.verify(22.0, "CALM", "UNAVAILABLE", None, 0, source="FIXED_DEMO")

    def test_mismatch_fails_verification(self) -> None:
        result = verify_ac_decision(
            {"source": "REAL_API", "temperature_c": 35.0},
            "CALM",
            {
                "decision": "COOL",
                "target_temperature": 27,
                "emotion_adjustment_c": 0,
            },
        )
        self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main()
