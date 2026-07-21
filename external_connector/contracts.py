"""Pure contracts for the sole INTERNET egress boundary.

This module intentionally contains no HTTP client and performs no network I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, JsonValue, ValidationError, model_validator

from backend.app.privacy_guard import PrivacyDecision, check_payload
from backend.app.schemas.base import Identifier, StrictModel, require_aware_datetime
from backend.app.schemas.events import NetworkScope, NetworkScopeValue
from backend.app.schemas.network import NetworkDestination


CONNECTOR_TIMEOUT_MS = 5000
MAX_RESPONSE_BYTES = 1_048_576
MAX_ATTEMPTS = 1
INTERNET_TARGETS = {
    NetworkDestination.WEATHER_API,
    NetworkDestination.PUBLIC_MUSIC_API,
}
NetworkDestinationValue = Annotated[NetworkDestination, Field(strict=False)]


class ConnectorAuditStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class ConnectorResponseStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ExternalConnectorRequest(StrictModel):
    request_id: Identifier
    source_agent: Identifier
    destination: NetworkDestinationValue
    network_scope: NetworkScopeValue
    payload: dict[str, JsonValue]
    timeout_ms: Literal[5000] = CONNECTOR_TIMEOUT_MS
    max_response_bytes: Literal[1048576] = MAX_RESPONSE_BYTES
    max_attempts: Literal[1] = MAX_ATTEMPTS
    created_at: datetime

    @model_validator(mode="after")
    def validate_boundary(self) -> "ExternalConnectorRequest":
        require_aware_datetime(self.created_at, "created_at")
        if self.network_scope is not NetworkScope.INTERNET:
            raise ValueError("external-connector only accepts INTERNET requests")
        if self.destination not in INTERNET_TARGETS:
            raise ValueError("destination is not on the INTERNET target allowlist")
        return self


class ApprovedExternalRequest(StrictModel):
    request_id: Identifier
    source_agent: Identifier
    destination: NetworkDestinationValue
    network_scope: NetworkScopeValue
    payload: dict[str, JsonValue]
    timeout_ms: Literal[5000]
    max_response_bytes: Literal[1048576]
    max_attempts: Literal[1]
    created_at: datetime

    @model_validator(mode="after")
    def validate_approved(self) -> "ApprovedExternalRequest":
        require_aware_datetime(self.created_at, "created_at")
        if self.network_scope is not NetworkScope.INTERNET:
            raise ValueError("approved connector request must be INTERNET")
        return self


class ExternalConnectorResponse(StrictModel):
    request_id: Identifier
    destination: NetworkDestinationValue
    network_scope: NetworkScopeValue
    status: Annotated[ConnectorResponseStatus, Field(strict=False)]
    body: JsonValue | None
    response_size_bytes: int = Field(ge=0, le=MAX_RESPONSE_BYTES)
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_response(self) -> "ExternalConnectorResponse":
        if self.network_scope is not NetworkScope.INTERNET:
            raise ValueError("connector response must be INTERNET")
        if self.destination not in INTERNET_TARGETS:
            raise ValueError("response destination is not allowlisted")
        return self


class ConnectorAuditRecord(StrictModel):
    request_id: str
    destination: str
    network_scope: str
    status: Annotated[ConnectorAuditStatus, Field(strict=False)]
    privacy_result: str
    violation_paths: list[str]
    payload_digest: str
    created_at: datetime

    @model_validator(mode="after")
    def validate_timestamp(self) -> "ConnectorAuditRecord":
        require_aware_datetime(self.created_at, "created_at")
        return self


class AuditSink(Protocol):
    def record(self, record: ConnectorAuditRecord) -> None:
        """Persist or display an audit record without raw sensitive payloads."""


class ConnectorRejectedError(ValueError):
    """Raised before any network operation can occur."""


def _payload_digest(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _safe_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if isinstance(value, StrEnum):
        value = value.value
    if not isinstance(value, str):
        return "UNKNOWN"
    if key == "request_id" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        return value
    if key == "destination" and value in {item.value for item in NetworkDestination}:
        return value
    if key == "network_scope" and value in {item.value for item in NetworkScope}:
        return value
    return "UNKNOWN"


class ExternalConnectorBoundary:
    """Validate, privacy-check, and audit an outbound request without sending it."""

    def __init__(self, audit_sink: AuditSink) -> None:
        self._audit_sink = audit_sink

    def prepare(self, raw_request: Mapping[str, Any]) -> ApprovedExternalRequest:
        digest = _payload_digest(raw_request.get("payload"))
        try:
            request = ExternalConnectorRequest.model_validate(raw_request)
        except ValidationError as error:
            paths = ["$." + ".".join(str(part) for part in item["loc"]) for item in error.errors()]
            self._record_rejection(raw_request, digest, "SCHEMA_REJECTED", paths)
            raise ConnectorRejectedError("connector request schema rejected") from error

        privacy_result = check_payload(
            request.network_scope,
            request.destination,
            request.payload,
        )
        if privacy_result.decision is PrivacyDecision.REJECTED:
            paths = [violation.path for violation in privacy_result.violations]
            self._record_rejection(request.model_dump(), digest, "PRIVACY_REJECTED", paths)
            raise ConnectorRejectedError("privacy guard rejected request")

        approved = ApprovedExternalRequest(
            request_id=request.request_id,
            source_agent=request.source_agent,
            destination=request.destination,
            network_scope=request.network_scope,
            payload=privacy_result.allowed_payload or {},
            timeout_ms=request.timeout_ms,
            max_response_bytes=request.max_response_bytes,
            max_attempts=request.max_attempts,
            created_at=request.created_at,
        )
        self._audit_sink.record(
            ConnectorAuditRecord(
                request_id=request.request_id,
                destination=request.destination.value,
                network_scope=request.network_scope.value,
                status=ConnectorAuditStatus.ACCEPTED,
                privacy_result=PrivacyDecision.ALLOWED.value,
                violation_paths=[],
                payload_digest=digest,
                created_at=datetime.now(timezone.utc),
            )
        )
        return approved

    def _record_rejection(
        self,
        raw_request: Mapping[str, Any],
        digest: str,
        privacy_result: str,
        violation_paths: list[str],
    ) -> None:
        self._audit_sink.record(
            ConnectorAuditRecord(
                request_id=_safe_text(raw_request, "request_id"),
                destination=_safe_text(raw_request, "destination"),
                network_scope=_safe_text(raw_request, "network_scope"),
                status=ConnectorAuditStatus.REJECTED,
                privacy_result=privacy_result,
                violation_paths=violation_paths,
                payload_digest=digest,
                created_at=datetime.now(timezone.utc),
            )
        )


def validate_response_size(body: bytes) -> int:
    """Reject oversized bodies before parsing or returning them."""

    if not isinstance(body, bytes):
        raise TypeError("response body must be bytes")
    size = len(body)
    if size > MAX_RESPONSE_BYTES:
        raise ValueError("response exceeds maximum size")
    return size
