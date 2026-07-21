from __future__ import annotations

import unittest

from pydantic import ValidationError

from backend.app.schemas.events import NetworkScope
from backend.app.schemas.network import NetworkDestination
from external_connector.contracts import (
    MAX_RESPONSE_BYTES,
    ConnectorAuditRecord,
    ConnectorRejectedError,
    ConnectorResponseStatus,
    ExternalConnectorBoundary,
    ExternalConnectorRequest,
    ExternalConnectorResponse,
    validate_response_size,
)
from tests.helpers import NOW, connector_request


class ListAuditSink:
    def __init__(self) -> None:
        self.records: list[ConnectorAuditRecord] = []

    def record(self, record: ConnectorAuditRecord) -> None:
        self.records.append(record)


class ExternalConnectorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sink = ListAuditSink()
        self.boundary = ExternalConnectorBoundary(self.sink)

    def test_weather_request_is_approved_and_audited(self) -> None:
        approved = self.boundary.prepare(connector_request())
        self.assertEqual(approved.network_scope, NetworkScope.INTERNET)
        self.assertEqual(len(self.sink.records), 1)
        self.assertEqual(self.sink.records[0].status.value, "ACCEPTED")

    def test_public_music_request_is_approved(self) -> None:
        raw = connector_request(
            "PUBLIC_MUSIC_API",
            {"action": "play", "track_id": "calm_piano_01"},
        )
        approved = self.boundary.prepare(raw)
        self.assertEqual(approved.destination, NetworkDestination.PUBLIC_MUSIC_API)

    def test_noninternet_and_ac_requests_are_rejected(self) -> None:
        with self.subTest("local"):
            raw = connector_request()
            raw["network_scope"] = "LOCAL"
            with self.assertRaises(ConnectorRejectedError):
                self.boundary.prepare(raw)
        with self.subTest("ac"):
            raw = connector_request("AC_DEVICE", {"device_id": "ac-1"})
            with self.assertRaises(ConnectorRejectedError):
                self.boundary.prepare(raw)

    def test_privacy_rejection_is_audited_without_raw_payload(self) -> None:
        raw = connector_request(payload={"city_code": "310000", "nested": {"rawAudio": "bytes"}})
        with self.assertRaises(ConnectorRejectedError):
            self.boundary.prepare(raw)
        record = self.sink.records[-1]
        self.assertEqual(record.status.value, "REJECTED")
        self.assertTrue(record.payload_digest)
        self.assertNotIn("bytes", record.model_dump_json())

    def test_timeout_response_limit_and_attempts_are_fixed(self) -> None:
        for field, value in (
            ("timeout_ms", 6000),
            ("max_response_bytes", 2048),
            ("max_attempts", 2),
        ):
            with self.subTest(field):
                raw = connector_request()
                raw[field] = value
                with self.assertRaises(ValidationError):
                    ExternalConnectorRequest.model_validate(raw)

    def test_response_size_and_response_schema_boundaries(self) -> None:
        self.assertEqual(validate_response_size(b"x" * MAX_RESPONSE_BYTES), MAX_RESPONSE_BYTES)
        with self.assertRaises(ValueError):
            validate_response_size(b"x" * (MAX_RESPONSE_BYTES + 1))
        response = ExternalConnectorResponse(
            request_id="request-001",
            destination=NetworkDestination.WEATHER_API,
            network_scope=NetworkScope.INTERNET,
            status=ConnectorResponseStatus.SUCCEEDED,
            body={"synthetic": True},
            response_size_bytes=20,
            latency_ms=5,
        )
        self.assertEqual(response.status, ConnectorResponseStatus.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
