from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.app.api import DemoASGIApp
from backend.app.local_music import LocalMusicPlayer
from backend.app.mocks import MockStep3
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.step3 import StateLabel
from backend.app.schemas.actions import ActionAuthorization, AuthorizationStatus
from external_connector.weather import RealExternalConnector
from tests.phase1c.helpers import FixedClock, NOW, proposals
from tests.phase4.helpers import FakeWeatherTransport, make_synthetic_music_root


async def http_request(
    app: DemoASGIApp,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[bytes, bytes], bytes]:
    encoded = json.dumps(body).encode("utf-8") if body is not None else b""
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": encoded, "more_body": False})
    outgoing: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return await incoming.get()

    async def send(message: dict[str, Any]) -> None:
        outgoing.append(message)

    await app(
        {"type": "http", "method": method, "path": path, "headers": []},
        receive,
        send,
    )
    start = next(item for item in outgoing if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"")
        for item in outgoing
        if item["type"] == "http.response.body"
    )
    return int(start["status"]), dict(start["headers"]), response_body


class BrowserMusicDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.music_root = make_synthetic_music_root(root)
        self.audio = (self.music_root / "tracks" / "calm_piano_01.flac").read_bytes()
        self.player = LocalMusicPlayer(
            self.music_root,
            delivery_mode="BROWSER",
        )
        self.orchestrator = Orchestrator(
            clock=FixedClock(),
            persistence=SQLitePersistence(root / "demo.sqlite3"),
            live_connector=RealExternalConnector(
                transport=FakeWeatherTransport(), clock=FixedClock()
            ),
            live_music=self.player,
        )
        session = self.orchestrator.begin_live_session(
            perception_source="STATIC_SYNTHETIC", degraded_reasons=[]
        )
        session = self.orchestrator.continue_live_pipeline(
            session.session_id,
            transcript="Synthetic user feels tired.",
            interaction_source="TEXT_FALLBACK",
            step3_output=MockStep3().analyze(),
            model_source="RULE_FALLBACK",
        )
        self.session = self.orchestrator.clarify(
            session.session_id, StateLabel.PHYSICAL_FATIGUE
        )
        self.action_id = self.session.music_action.action_id
        self.app = DemoASGIApp(self.orchestrator)

    async def asyncTearDown(self) -> None:
        self.player.close()
        self.temporary.cleanup()

    async def test_audio_requires_authorization_and_is_consumed_once(self) -> None:
        path = (
            f"/v1/music/sessions/{self.session.session_id}/actions/"
            f"{self.action_id}/audio"
        )
        status, _, body = await http_request(self.app, "GET", path)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "MUSIC_AUDIO_NOT_READY")

        status, _, snapshot_raw = await http_request(
            self.app,
            "POST",
            (
                f"/v1/live/sessions/{self.session.session_id}/actions/"
                f"{self.action_id}/authorization"
            ),
            {"approved": True},
        )
        self.assertEqual(status, 200)
        result = json.loads(snapshot_raw)["results"][self.action_id]["result"]
        self.assertTrue(result["delivery_ready"])
        self.assertFalse(result["playback_started"])
        self.assertFalse(result["physical_action_performed"])

        status, headers, body = await http_request(self.app, "GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(headers[b"content-type"], b"audio/flac")
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual(body, self.audio)

        status, _, body = await http_request(self.app, "GET", path)
        self.assertEqual(status, 410)
        self.assertEqual(json.loads(body)["error"], "MUSIC_AUDIO_CONSUMED")

    async def test_audio_bytes_are_not_persisted(self) -> None:
        self.orchestrator.authorize(
            self.session.session_id, self.action_id, True
        )
        database = self.orchestrator.persistence.path.read_bytes()
        self.assertNotIn(self.audio, database)
        self.assertEqual(self.player.health()["pending_deliveries"], 1)

    async def test_audius_preview_keeps_provider_content_type_in_memory(self) -> None:
        proposal, _ = proposals(session_id="session-browser-preview")
        authorization = ActionAuthorization(
            action_id=proposal.action_id,
            action_type=proposal.action_type,
            authorization_status=AuthorizationStatus.APPROVED,
            decided_at=NOW,
            expires_at=proposal.expires_at,
        )
        result = self.player.execute_preview(
            proposal,
            authorization,
            NOW,
            audio=b"synthetic-audius-preview",
            provider_track_id="trackA",
            size_bytes=24,
            fetch_latency_ms=7,
            content_type="audio/mpeg",
        )
        self.assertTrue(result.result["delivery_ready"])
        self.assertFalse(result.result["playback_started"])
        delivery = self.player.consume_browser_delivery(proposal.action_id)
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery.content_type, "audio/mpeg")
        self.assertEqual(delivery.audio, b"synthetic-audius-preview")


if __name__ == "__main__":
    unittest.main()
