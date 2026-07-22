from __future__ import annotations

import asyncio
import json
import unittest
from collections import deque
from datetime import datetime, timezone
from typing import Any

from external_connector.audius import AudiusMusicConnector, AudiusSettings
from external_connector.client import (
    InternalConnectorHTTP,
    InternalHTTPResponse,
    RemoteAudiusConnector,
    RemoteWeatherConnector,
)
from external_connector.service import ExternalConnectorASGIApp
from external_connector.weather import RealExternalConnector
from backend.app.schemas.music import PlaylistKey
from tests.helpers import connector_request
from tests.phase4.helpers import FakeWeatherTransport


async def raw_request(
    app: ExternalConnectorASGIApp,
    method: str,
    path: str,
    body: dict[str, Any] | bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    if isinstance(body, dict):
        encoded = json.dumps(body, default=str).encode("utf-8")
    elif isinstance(body, bytes):
        encoded = body
    else:
        encoded = b""
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
    headers = {
        key.decode("ascii"): value.decode("ascii") for key, value in start["headers"]
    }
    response_body = b"".join(
        item.get("body", b"")
        for item in outgoing
        if item["type"] == "http.response.body"
    )
    return int(start["status"]), headers, response_body


class ConnectorServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.weather_transport = FakeWeatherTransport()
        weather = RealExternalConnector(
            transport=self.weather_transport,
            clock=lambda: datetime(2026, 7, 21, tzinfo=timezone.utc),
        )
        audius = AudiusMusicConnector(
            AudiusSettings(False, "", "", {}, False)
        )
        self.app = ExternalConnectorASGIApp(weather=weather, audius=audius)

    async def test_health_never_probes_provider_and_reports_placeholder_audius(self) -> None:
        status, _, raw = await raw_request(self.app, "GET", "/health")
        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertFalse(body["provider_probe_performed"])
        self.assertEqual(body["audius"]["status"], "NOT_CONFIGURED")
        self.assertEqual(self.weather_transport.calls, [])

    async def test_weather_request_crosses_privacy_boundary_once(self) -> None:
        status, _, raw = await raw_request(
            self.app, "POST", "/v1/weather", connector_request()
        )
        body = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(body["body"]["city_code"], "310000")
        self.assertEqual(len(self.weather_transport.calls), 1)

    async def test_privacy_injection_is_rejected_before_transport(self) -> None:
        request = connector_request(
            payload={"city_code": "310000", "nested": {"raw_audio": "synthetic"}}
        )
        status, _, raw = await raw_request(self.app, "POST", "/v1/weather", request)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["error"], "CONNECTOR_REJECTED")
        self.assertEqual(self.weather_transport.calls, [])

    async def test_unconfigured_audius_never_sends_request(self) -> None:
        request = connector_request(
            destination="PUBLIC_MUSIC_API",
            payload={"action": "sync_playlist", "playlist_ref": "RELAX"},
        )
        status, _, raw = await raw_request(
            self.app, "POST", "/v1/audius/playlists/RELAX/sync", request
        )
        body = json.loads(raw)
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "PLAYLIST_NOT_CONFIGURED", "request_sent": False})

    async def test_invalid_json_is_distinct_from_schema_rejection(self) -> None:
        status, _, raw = await raw_request(
            self.app, "POST", "/v1/weather", b"{invalid"
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"], "INVALID_JSON")


class FakeInternalTransport:
    def __init__(self, *responses: InternalHTTPResponse) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None,
        *,
        max_response_bytes: int,
    ) -> InternalHTTPResponse:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "body": body,
                "max_response_bytes": max_response_bytes,
            }
        )
        return self.responses.popleft()


def json_response(status: int, body: dict[str, object]) -> InternalHTTPResponse:
    return InternalHTTPResponse(
        status,
        {"content-type": "application/json"},
        json.dumps(body).encode("utf-8"),
        1,
    )


class RemoteConnectorClientTests(unittest.TestCase):
    def test_internal_transport_rejects_public_and_https_destinations(self) -> None:
        for value in (
            "https://external-connector:8030",
            "http://api.open-meteo.com",
            "http://external-connector:8030/path",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                InternalConnectorHTTP(value)

    def test_remote_weather_validates_locally_then_validates_response(self) -> None:
        response = {
            "request_id": "request-001",
            "destination": "WEATHER_API",
            "network_scope": "INTERNET",
            "status": "SUCCEEDED",
            "body": {"city_code": "310000", "temperature_c": 25},
            "response_size_bytes": 32,
            "latency_ms": 4,
        }
        transport = FakeInternalTransport(json_response(200, response))
        connector = RemoteWeatherConnector(transport=transport)
        result = connector.send(connector_request())
        self.assertEqual(result.body["city_code"], "310000")
        self.assertEqual(transport.requests[0]["path"], "/v1/weather")

    def test_remote_weather_privacy_rejection_never_reaches_service(self) -> None:
        transport = FakeInternalTransport()
        connector = RemoteWeatherConnector(transport=transport)
        request = connector_request(payload={"city_code": "310000", "raw_video": "x"})
        with self.assertRaises(ValueError):
            connector.send(request)
        self.assertEqual(transport.requests, [])

    def test_remote_audius_health_exposes_only_configured_category_names(self) -> None:
        transport = FakeInternalTransport(
            json_response(
                200,
                {
                    "audius": {
                        "component": "AUDIUS_MUSIC",
                        "available": True,
                        "status": "CONFIGURED_NOT_PROBED",
                        "latency_ms": 0,
                        "preview_ready": True,
                        "sync_ready": True,
                        "verified_seed_count": 13,
                        "configured_categories": ["RELAX"],
                    }
                },
            )
        )
        connector = RemoteAudiusConnector(transport=transport)
        self.assertTrue(connector.settings.preview_ready)
        self.assertTrue(connector.settings.configured_for(PlaylistKey.RELAX))
        self.assertNotIn("api_key", connector.health())

    def test_remote_audius_sync_preserves_bounded_snapshot_contract(self) -> None:
        transport = FakeInternalTransport(
            json_response(
                200,
                {
                    "playlist_key": "RELAX",
                    "provider_playlist_id": "playlist001",
                    "track_ids": ["trackA", "trackB"],
                    "source_count": 2,
                    "truncated": False,
                    "latency_ms": 8,
                },
            )
        )
        connector = RemoteAudiusConnector(transport=transport)
        request = connector_request(
            destination="PUBLIC_MUSIC_API",
            payload={"action": "sync_playlist", "playlist_ref": "RELAX"},
        )
        result = connector.sync_playlist(request, PlaylistKey.RELAX)
        self.assertEqual(result.track_ids, ("trackA", "trackB"))
        self.assertEqual(
            transport.requests[0]["path"], "/v1/audius/playlists/RELAX/sync"
        )

    def test_remote_audius_preview_accepts_only_validated_audio_envelope(self) -> None:
        audio = b"RIFF" + b"\x00" * 48
        transport = FakeInternalTransport(
            InternalHTTPResponse(
                200,
                {
                    "content-type": "audio/wav",
                    "x-provider-track-id": "trackA",
                    "x-preview-latency-ms": "9",
                    "x-preview-size-bytes": str(len(audio)),
                },
                audio,
                10,
            )
        )
        connector = RemoteAudiusConnector(transport=transport)
        request = connector_request(
            destination="PUBLIC_MUSIC_API",
            payload={"action": "play", "track_id": "calm_piano_01"},
        )
        result = connector.fetch_preview(request, "trackA")
        self.assertEqual(result.audio, audio)
        self.assertEqual(result.size_bytes, len(audio))

    def test_remote_audius_safe_error_preserves_request_sent_flag(self) -> None:
        transport = FakeInternalTransport(
            json_response(
                502,
                {"error": "AUDIUS_CONTENT_HTTP_503", "request_sent": True},
            )
        )
        connector = RemoteAudiusConnector(transport=transport)
        request = connector_request(
            destination="PUBLIC_MUSIC_API",
            payload={"action": "play", "track_id": "calm_piano_01"},
        )
        with self.assertRaisesRegex(RuntimeError, "AUDIUS_CONTENT_HTTP_503") as caught:
            connector.fetch_preview(request, "trackA")
        self.assertTrue(caught.exception.request_sent)
