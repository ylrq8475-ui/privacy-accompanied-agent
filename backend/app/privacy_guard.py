"""Recursive payload inspection and route-specific allowlist validation."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import JsonValue, TypeAdapter, ValidationError

from backend.app.schemas.base import StrictModel
from backend.app.schemas.events import NetworkScope
from backend.app.schemas.network import (
    ACPayload,
    MusicPayload,
    NetworkDestination,
    PublicMusicPayload,
    WeatherPayload,
)


FORBIDDEN_FIELDS = frozenset(
    {
        "raw_audio",
        "raw_video",
        "emotion",
        "state_hypotheses",
        "user_schedule",
        "arrival_history",
        "memory_content",
        "recommendation_reason",
        "conversation_history",
    }
)

PAYLOAD_ALLOWLIST = {
    (NetworkScope.INTERNET, NetworkDestination.WEATHER_API): WeatherPayload,
    (NetworkScope.INTERNET, NetworkDestination.PUBLIC_MUSIC_API): PublicMusicPayload,
    (NetworkScope.LOCAL, NetworkDestination.LOCAL_MUSIC): MusicPayload,
    (NetworkScope.LOCAL, NetworkDestination.AC_DEVICE): ACPayload,
    (NetworkScope.LAN, NetworkDestination.AC_DEVICE): ACPayload,
}


class PrivacyDecision(StrEnum):
    ALLOWED = "ALLOWED"
    REJECTED = "REJECTED"


class PrivacyViolation(StrictModel):
    path: str
    field: str
    reason: str


class PrivacyCheckResult(StrictModel):
    decision: PrivacyDecision
    allowed_payload: dict[str, JsonValue] | None
    violations: list[PrivacyViolation]


def normalize_field_name(value: str) -> str:
    """Normalize case, separators, and camelCase before sensitive-key matching."""

    with_word_boundaries = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value.strip())
    return re.sub(r"[\s-]+", "_", with_word_boundaries).casefold()


def _scan_forbidden_fields(value: Any, path: str = "$") -> list[PrivacyViolation]:
    violations: list[PrivacyViolation] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            normalized = normalize_field_name(key)
            if normalized in FORBIDDEN_FIELDS:
                violations.append(
                    PrivacyViolation(path=child_path, field=normalized, reason="forbidden_field")
                )
            violations.extend(_scan_forbidden_fields(child, child_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            violations.extend(_scan_forbidden_fields(child, f"{path}[{index}]"))
    return violations


def check_payload(
    network_scope: NetworkScope,
    destination: NetworkDestination,
    payload: Any,
) -> PrivacyCheckResult:
    """Inspect nested keys, then validate the exact route payload schema."""

    violations = _scan_forbidden_fields(payload)
    if violations:
        return PrivacyCheckResult(
            decision=PrivacyDecision.REJECTED,
            allowed_payload=None,
            violations=violations,
        )

    payload_model = PAYLOAD_ALLOWLIST.get((network_scope, destination))
    if payload_model is None:
        return PrivacyCheckResult(
            decision=PrivacyDecision.REJECTED,
            allowed_payload=None,
            violations=[
                PrivacyViolation(path="$", field="route", reason="route_not_allowlisted")
            ],
        )

    try:
        # The model remains strict for ordinary fields; enum fields explicitly
        # accept their exact JSON string representation.
        validated = TypeAdapter(payload_model).validate_python(payload)
    except ValidationError as error:
        schema_violations = [
            PrivacyViolation(
                path="$" + "".join(f".{part}" for part in item["loc"]),
                field=str(item["loc"][-1]) if item["loc"] else "payload",
                reason=item["type"],
            )
            for item in error.errors(include_url=False, include_input=False)
        ]
        return PrivacyCheckResult(
            decision=PrivacyDecision.REJECTED,
            allowed_payload=None,
            violations=schema_violations,
        )

    return PrivacyCheckResult(
        decision=PrivacyDecision.ALLOWED,
        allowed_payload=validated.model_dump(mode="json"),
        violations=[],
    )
