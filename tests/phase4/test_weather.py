from __future__ import annotations

import sqlite3
import tempfile
import unittest
import urllib.parse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from external_connector.contracts import ConnectorRejectedError
from external_connector.weather import (
    ExternalConnectorTransportError,
    RealExternalConnector,
    UrllibWeatherTransport,
    WEATHER_CITY_COORDINATES,
)

from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.phase4 import WeatherSnapshot, WeatherSource
from tests.phase1c.helpers import FixedClock, NOW
from tests.phase4.helpers import FakeWeatherTransport


def request(payload: dict[str, object]) -> dict[str, object]:
    return {
        "request_id": "request-phase4-weather",
        "source_agent": "weather-agent",
        "destination": "WEATHER_API",
        "network_scope": "INTERNET",
        "payload": payload,
        "created_at": NOW,
    }


class Phase4WeatherTests(unittest.TestCase):
    def test_real_connector_sends_only_approved_city_code(self) -> None:
        transport = FakeWeatherTransport()
        connector = RealExternalConnector(transport=transport, clock=FixedClock())
        response = connector.send(request({"city_code": "310000"}))

        self.assertEqual(connector.sent_requests[0].payload, {"city_code": "310000"})
        self.assertEqual(len(transport.calls), 1)
        url, timeout = transport.calls[0]
        self.assertTrue(url.startswith("https://api.open-meteo.com/v1/forecast?"))
        self.assertEqual(timeout, 5.0)
        self.assertEqual(response.body["source"], "REAL_API")
        self.assertEqual(response.body["condition"], "partly_cloudy")

    def test_each_supported_city_maps_to_its_allowlisted_coordinates(self) -> None:
        for city_code, expected_coordinates in WEATHER_CITY_COORDINATES.items():
            with self.subTest(city_code=city_code):
                transport = FakeWeatherTransport()
                connector = RealExternalConnector(transport=transport, clock=FixedClock())
                response = connector.send(request({"city_code": city_code}))
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0][0]).query)
                self.assertEqual(
                    (query["latitude"][0], query["longitude"][0]),
                    expected_coordinates,
                )
                self.assertEqual(response.body["city_code"], city_code)

    def test_unknown_city_is_rejected_before_transport(self) -> None:
        transport = FakeWeatherTransport()
        connector = RealExternalConnector(transport=transport)
        with self.assertRaises(ConnectorRejectedError):
            connector.send(request({"city_code": "999999"}))
        self.assertEqual(transport.calls, [])

    def test_privacy_rejection_happens_before_transport(self) -> None:
        transport = FakeWeatherTransport()
        connector = RealExternalConnector(transport=transport)
        with self.assertRaises(ConnectorRejectedError):
            connector.send(
                request(
                    {
                        "city_code": "310000",
                        "raw_audio": "synthetic-forbidden",
                    }
                )
            )
        self.assertEqual(transport.calls, [])
        self.assertEqual(connector.sent_requests, [])
        self.assertEqual(connector.audit_sink.records[-1].status.value, "REJECTED")

    def test_malformed_provider_response_is_rejected(self) -> None:
        connector = RealExternalConnector(
            transport=FakeWeatherTransport(b'{"current":{"temperature_2m":"25"}}')
        )
        with self.assertRaises(ExternalConnectorTransportError):
            connector.send(request({"city_code": "310000"}))

        oversized = RealExternalConnector(
            transport=FakeWeatherTransport(b"x" * 1_048_577)
        )
        with self.assertRaises(ExternalConnectorTransportError):
            oversized.send(request({"city_code": "310000"}))

    def test_transport_rejects_non_allowlisted_url_before_io(self) -> None:
        transport = UrllibWeatherTransport()
        with self.assertRaises(ExternalConnectorTransportError):
            transport.fetch("https://example.com/v1/forecast", 5.0)

    def test_public_weather_host_exists_only_in_external_connector(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        backend_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (repository / "backend").rglob("*.py")
        )
        connector_text = (repository / "external_connector" / "weather.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("api.open-meteo.com", backend_text)
        self.assertIn("api.open-meteo.com", connector_text)

    def test_timestamped_weather_cache_survives_reopen_without_raw_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "demo.sqlite3"
            store = SQLitePersistence(path)
            snapshot = WeatherSnapshot(
                city_code="310000",
                temperature_c=25.5,
                condition="partly_cloudy",
                source=WeatherSource.REAL_API,
                fetched_at=NOW,
                provider="OPEN_METEO",
            )
            store.save_weather_cache(snapshot)
            cached = SQLitePersistence(path).get_weather_cache("310000")
            self.assertEqual(cached.source, WeatherSource.CACHE)
            self.assertEqual(cached.fetched_at, NOW)
            with closing(sqlite3.connect(path)) as connection:
                serialized = " ".join(
                    str(value)
                    for row in connection.execute("SELECT * FROM weather_cache")
                    for value in row
                )
            self.assertNotIn("current", serialized)
            self.assertNotIn("weather_code", serialized)

    def test_weather_cache_is_isolated_by_city(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLitePersistence(Path(temporary) / "demo.sqlite3")
            for city_code, temperature in (("110000", 18.0), ("440300", 29.0)):
                store.save_weather_cache(
                    WeatherSnapshot(
                        city_code=city_code,
                        temperature_c=temperature,
                        condition="clear",
                        source=WeatherSource.REAL_API,
                        fetched_at=NOW,
                        provider="OPEN_METEO",
                    )
                )
            self.assertEqual(store.get_weather_cache("110000").temperature_c, 18.0)
            self.assertEqual(store.get_weather_cache("440300").temperature_c, 29.0)
            self.assertIsNone(store.get_weather_cache("510100"))

    def test_live_weather_falls_back_to_cache_then_fixed_demo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "demo.sqlite3"
            persistence = SQLitePersistence(path)
            persistence.save_weather_cache(
                WeatherSnapshot(
                    city_code="310000",
                    temperature_c=19.0,
                    condition="rain",
                    source=WeatherSource.REAL_API,
                    fetched_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
                    provider="OPEN_METEO",
                )
            )
            failing = RealExternalConnector(
                transport=FakeWeatherTransport(fail=True), clock=FixedClock()
            )
            orchestrator = Orchestrator(
                clock=FixedClock(), persistence=persistence, live_connector=failing
            )
            cached_session = orchestrator.begin_live_session(
                perception_source="STATIC_SYNTHETIC", degraded_reasons=[]
            )
            context = next(
                event
                for event in orchestrator.audit_log.list_events(cached_session.session_id)
                if event.event_type == "STATE_TRANSITION"
                and event.payload["to_state"] == "CONTEXT_READY"
            )
            self.assertEqual(context.payload["details"]["weather"]["source"], "CACHE")

            empty_path = Path(temporary) / "empty.sqlite3"
            empty = Orchestrator(
                clock=FixedClock(),
                persistence=SQLitePersistence(empty_path),
                live_connector=RealExternalConnector(
                    transport=FakeWeatherTransport(fail=True), clock=FixedClock()
                ),
            )
            fixed_session = empty.begin_live_session(
                perception_source="STATIC_SYNTHETIC", degraded_reasons=[]
            )
            fixed_context = next(
                event
                for event in empty.audit_log.list_events(fixed_session.session_id)
                if event.event_type == "STATE_TRANSITION"
                and event.payload["to_state"] == "CONTEXT_READY"
            )
            self.assertEqual(
                fixed_context.payload["details"]["weather"]["source"], "FIXED_DEMO"
            )


if __name__ == "__main__":
    unittest.main()
