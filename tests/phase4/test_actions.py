from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from external_connector.weather import RealExternalConnector

from backend.app.local_music import LocalMusicError, LocalMusicPlayer
from backend.app.mocks import MockStep3
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.privacy_guard import PrivacyDecision, check_payload
from backend.app.schemas.actions import (
    ActionAuthorization,
    AuthorizationStatus,
)
from backend.app.schemas.step3 import StateLabel
from backend.app.schemas.events import NetworkScope
from backend.app.schemas.network import NetworkDestination
from tests.phase1c.helpers import FixedClock, NOW, proposals
from tests.phase4.helpers import FakeWeatherTransport, RecordingPlaybackBackend


class Phase4ActionTests(unittest.TestCase):
    def test_local_ac_mock_payload_is_allowlisted_without_connector(self) -> None:
        result = check_payload(
            NetworkScope.LOCAL,
            NetworkDestination.AC_DEVICE,
            {
                "device_id": "living_room_ac_mock",
                "mode": "heat",
                "target_temperature": 24,
                "duration_minutes": 30,
            },
        )
        self.assertEqual(result.decision, PrivacyDecision.ALLOWED)

    def test_local_player_rechecks_approval_expiry_and_duplicate_action(self) -> None:
        backend = RecordingPlaybackBackend()
        player = LocalMusicPlayer(backend=backend)
        music, _ = proposals(session_id="session-local-player")
        pending = ActionAuthorization(
            action_id=music.action_id,
            action_type=music.action_type,
            authorization_status=AuthorizationStatus.PENDING,
            decided_at=None,
            expires_at=music.expires_at,
        )
        with self.assertRaises(LocalMusicError):
            player.execute(music, pending, NOW)
        self.assertEqual(backend.paths, [])

        approved = ActionAuthorization(
            action_id=music.action_id,
            action_type=music.action_type,
            authorization_status=AuthorizationStatus.APPROVED,
            decided_at=NOW,
            expires_at=music.expires_at,
        )
        result = player.execute(music, approved, NOW)
        self.assertTrue(result.result["playback_started"])
        self.assertFalse(result.result["mock"])
        self.assertEqual(len(backend.paths), 1)
        with self.assertRaises(LocalMusicError):
            player.execute(music, approved, NOW)
        self.assertEqual(len(backend.paths), 1)

        later_music, _ = proposals(
            session_id="session-expired-player",
            proposed_at=NOW - timedelta(minutes=10),
            expires_at=NOW - timedelta(minutes=5),
        )
        expired = ActionAuthorization(
            action_id=later_music.action_id,
            action_type=later_music.action_type,
            authorization_status=AuthorizationStatus.APPROVED,
            decided_at=NOW - timedelta(minutes=6),
            expires_at=later_music.expires_at,
        )
        with self.assertRaises(LocalMusicError):
            player.execute(later_music, expired, NOW)

    def test_live_music_is_local_and_does_not_approve_ac(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = RecordingPlaybackBackend()
            orchestrator = Orchestrator(
                clock=FixedClock(),
                persistence=SQLitePersistence(Path(temporary) / "demo.sqlite3"),
                live_connector=RealExternalConnector(
                    transport=FakeWeatherTransport(), clock=FixedClock()
                ),
                live_music=LocalMusicPlayer(backend=backend),
            )
            session = orchestrator.begin_live_session(
                perception_source="STATIC_SYNTHETIC", degraded_reasons=[]
            )
            session = orchestrator.continue_live_pipeline(
                session.session_id,
                transcript="Synthetic user feels tired.",
                interaction_source="TEXT_FALLBACK",
                step3_output=MockStep3().analyze(),
                model_source="RULE_FALLBACK",
            )
            session = orchestrator.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
            music_id = session.music_action.action_id
            ac_id = session.ac_action.action_id
            session = orchestrator.authorize(session.session_id, music_id, True)

            self.assertEqual(len(backend.paths), 1)
            self.assertEqual(
                session.authorizations[ac_id].authorization_status,
                AuthorizationStatus.PENDING,
            )
            local_event = next(
                event
                for event in orchestrator.audit_log.list_events(session.session_id)
                if event.event_type == "LOCAL_ACTION_REQUEST"
            )
            self.assertEqual(
                local_event.payload["outbound_payload"],
                {"action": "play", "track_id": "emotion_relax_01"},
            )
            self.assertFalse(local_event.payload["via_external_connector"])

            session = orchestrator.authorize(session.session_id, ac_id, True)
            ac_result = session.results[ac_id].result
            self.assertEqual(ac_result["message"], "模拟执行成功")
            self.assertTrue(ac_result["mock"])
            self.assertFalse(ac_result["physical_action_performed"])

    def test_rejected_music_never_reaches_player(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = RecordingPlaybackBackend()
            orchestrator = Orchestrator(
                clock=FixedClock(),
                persistence=SQLitePersistence(Path(temporary) / "demo.sqlite3"),
                live_connector=RealExternalConnector(
                    transport=FakeWeatherTransport(), clock=FixedClock()
                ),
                live_music=LocalMusicPlayer(backend=backend),
            )
            session = orchestrator.begin_live_session(
                perception_source="STATIC_SYNTHETIC", degraded_reasons=[]
            )
            session = orchestrator.continue_live_pipeline(
                session.session_id,
                transcript="Synthetic user feels tired.",
                interaction_source="TEXT_FALLBACK",
                step3_output=MockStep3().analyze(),
                model_source="RULE_FALLBACK",
            )
            session = orchestrator.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
            orchestrator.authorize(session.session_id, session.music_action.action_id, False)
            self.assertEqual(backend.paths, [])


if __name__ == "__main__":
    unittest.main()
