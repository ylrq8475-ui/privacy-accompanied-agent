from __future__ import annotations

import unittest

from backend.app.privacy_guard import PrivacyDecision, check_payload, normalize_field_name
from backend.app.schemas.events import NetworkScope
from backend.app.schemas.network import NetworkDestination


class PrivacyGuardTests(unittest.TestCase):
    def test_weather_payload_is_allowlisted(self) -> None:
        result = check_payload(
            NetworkScope.INTERNET,
            NetworkDestination.WEATHER_API,
            {"city_code": "310000"},
        )
        self.assertEqual(result.decision, PrivacyDecision.ALLOWED)

    def test_supported_weather_cities_are_allowlisted_and_unknown_city_is_rejected(self) -> None:
        for city_code in ("110000", "310000", "440100", "440300", "510100"):
            with self.subTest(city_code=city_code):
                result = check_payload(
                    NetworkScope.INTERNET,
                    NetworkDestination.WEATHER_API,
                    {"city_code": city_code},
                )
                self.assertEqual(result.decision, PrivacyDecision.ALLOWED)
        rejected = check_payload(
            NetworkScope.INTERNET,
            NetworkDestination.WEATHER_API,
            {"city_code": "999999"},
        )
        self.assertEqual(rejected.decision, PrivacyDecision.REJECTED)

    def test_public_music_payload_is_allowlisted(self) -> None:
        result = check_payload(
            NetworkScope.INTERNET,
            NetworkDestination.PUBLIC_MUSIC_API,
            {"action": "play", "track_id": "calm_piano_01"},
        )
        self.assertEqual(result.decision, PrivacyDecision.ALLOWED)

    def test_local_music_and_lan_ac_are_allowlisted(self) -> None:
        with self.subTest("local_music"):
            result = check_payload(
                NetworkScope.LOCAL,
                NetworkDestination.LOCAL_MUSIC,
                {"action": "play", "track_id": "calm_piano_01"},
            )
            self.assertEqual(result.decision, PrivacyDecision.ALLOWED)
        with self.subTest("lan_ac"):
            result = check_payload(
                NetworkScope.LAN,
                NetworkDestination.AC_DEVICE,
                {
                    "device_id": "living_room_ac",
                    "mode": "heat",
                    "target_temperature": 24,
                    "duration_minutes": 30,
                },
            )
            self.assertEqual(result.decision, PrivacyDecision.ALLOWED)

    def test_missing_allowlisted_field_is_rejected(self) -> None:
        result = check_payload(
            NetworkScope.INTERNET,
            NetworkDestination.WEATHER_API,
            {},
        )
        self.assertEqual(result.decision, PrivacyDecision.REJECTED)

    def test_extra_field_is_rejected(self) -> None:
        result = check_payload(
            NetworkScope.INTERNET,
            NetworkDestination.WEATHER_API,
            {"city_code": "310000", "district": "synthetic"},
        )
        self.assertEqual(result.decision, PrivacyDecision.REJECTED)

    def test_invalid_payload_type_is_rejected(self) -> None:
        result = check_payload(
            NetworkScope.LAN,
            NetworkDestination.AC_DEVICE,
            {
                "device_id": "living_room_ac",
                "mode": "heat",
                "target_temperature": "24",
                "duration_minutes": 30,
            },
        )
        self.assertEqual(result.decision, PrivacyDecision.REJECTED)

    def test_nested_forbidden_dictionary_field_is_rejected(self) -> None:
        result = check_payload(
            NetworkScope.INTERNET,
            NetworkDestination.WEATHER_API,
            {"city_code": "310000", "context": {"raw_audio": "synthetic"}},
        )
        self.assertEqual(result.decision, PrivacyDecision.REJECTED)
        self.assertEqual(result.violations[0].path, "$.context.raw_audio")

    def test_nested_list_and_normalized_field_names_are_rejected(self) -> None:
        for field in ("rawAudio", "RAW-AUDIO", "conversation history"):
            with self.subTest(field):
                result = check_payload(
                    NetworkScope.INTERNET,
                    NetworkDestination.WEATHER_API,
                    {"city_code": "310000", "items": [{field: "synthetic"}]},
                )
                self.assertEqual(result.decision, PrivacyDecision.REJECTED)
        self.assertEqual(normalize_field_name("RawAudio"), "raw_audio")

    def test_values_do_not_false_positive_and_target_mismatch_rejects(self) -> None:
        with self.subTest("value"):
            result = check_payload(
                NetworkScope.LOCAL,
                NetworkDestination.AC_DEVICE,
                {
                    "device_id": "conversation_history_theme",
                    "mode": "heat",
                    "target_temperature": 24,
                    "duration_minutes": 30,
                },
            )
            self.assertEqual(result.decision, PrivacyDecision.ALLOWED)
        with self.subTest("target"):
            result = check_payload(
                NetworkScope.INTERNET,
                NetworkDestination.WEATHER_API,
                {"action": "play", "track_id": "calm_piano_01"},
            )
            self.assertEqual(result.decision, PrivacyDecision.REJECTED)


if __name__ == "__main__":
    unittest.main()
