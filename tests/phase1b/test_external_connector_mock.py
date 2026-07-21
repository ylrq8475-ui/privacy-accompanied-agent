from __future__ import annotations

import unittest
from datetime import datetime, timezone

from external_connector.contracts import ConnectorRejectedError
from external_connector.mock import MockConnectorTransportError, MockExternalConnector


def request(payload: dict | None = None) -> dict:
    return {
        "request_id": "request-phase1b",
        "source_agent": "weather-agent",
        "destination": "WEATHER_API",
        "network_scope": "INTERNET",
        "payload": payload or {"city_code": "310000"},
        "created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
    }


class ExternalConnectorMockTests(unittest.TestCase):
    def test_valid_weather_payload_reaches_mock_send_boundary(self) -> None:
        connector = MockExternalConnector()
        response = connector.send(request())
        self.assertEqual(len(connector.sent_requests), 1)
        self.assertEqual(connector.weather_service.requested_city_codes, ["310000"])
        self.assertTrue(response.body["mock"])
        self.assertEqual(response.network_scope.value, "INTERNET")

    def test_privacy_rejection_never_reaches_send_boundary(self) -> None:
        connector = MockExternalConnector()
        with self.assertRaises(ConnectorRejectedError):
            connector.send(
                request({"city_code": "310000", "nested": {"rawVideo": "synthetic"}})
            )
        self.assertEqual(len(connector.sent_requests), 0)
        self.assertEqual(connector.audit_sink.records[-1].privacy_result, "PRIVACY_REJECTED")

    def test_noninternet_request_never_reaches_send_boundary(self) -> None:
        connector = MockExternalConnector()
        raw = request()
        raw["network_scope"] = "LOCAL"
        with self.assertRaises(ConnectorRejectedError):
            connector.send(raw)
        self.assertEqual(len(connector.sent_requests), 0)

    def test_mock_transport_failure_is_not_reported_as_success(self) -> None:
        connector = MockExternalConnector()
        connector.fail_next = True
        with self.assertRaises(MockConnectorTransportError):
            connector.send(request())
        self.assertEqual(len(connector.sent_requests), 1)


if __name__ == "__main__":
    unittest.main()
