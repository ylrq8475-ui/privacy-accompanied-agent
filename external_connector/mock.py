"""Pure external-connector Mock; validates and audits but never accesses a network."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import (
    ConnectorAuditRecord,
    ExternalConnectorBoundary,
    ExternalConnectorResponse,
)
from backend.app.schemas.network import NetworkDestination


class ListConnectorAuditSink:
    def __init__(self) -> None:
        self.records: list[ConnectorAuditRecord] = []

    def record(self, record: ConnectorAuditRecord) -> None:
        self.records.append(record)


class MockConnectorTransportError(RuntimeError):
    pass


class MockWeatherService:
    """Deterministic public-weather substitute behind the connector Mock."""

    def __init__(self) -> None:
        self.requested_city_codes: list[str] = []

    def fetch(self, city_code: str) -> dict[str, str | int | bool]:
        self.requested_city_codes.append(city_code)
        return {
            "mock": True,
            "city_code": city_code,
            "condition": "clear",
            "temperature_c": 22,
        }


class MockExternalConnector:
    """A deterministic transport substitute with an observable send boundary."""

    def __init__(
        self,
        audit_sink: ListConnectorAuditSink | None = None,
        weather_service: MockWeatherService | None = None,
    ) -> None:
        self.audit_sink = audit_sink or ListConnectorAuditSink()
        self.boundary = ExternalConnectorBoundary(self.audit_sink)
        self.weather_service = weather_service or MockWeatherService()
        self.sent_requests = []
        self.fail_next = False

    def send(self, raw_request: Mapping[str, Any]) -> ExternalConnectorResponse:
        approved = self.boundary.prepare(raw_request)
        self.sent_requests.append(approved)
        if self.fail_next:
            self.fail_next = False
            raise MockConnectorTransportError("synthetic connector Mock failure")

        if approved.destination is NetworkDestination.WEATHER_API:
            body = self.weather_service.fetch(str(approved.payload["city_code"]))
        else:
            body = {"mock": True, "accepted": True}
        return ExternalConnectorResponse(
            request_id=approved.request_id,
            destination=approved.destination,
            network_scope=approved.network_scope,
            status="SUCCEEDED",
            body=body,
            response_size_bytes=len(str(body).encode("utf-8")),
            latency_ms=0,
        )
