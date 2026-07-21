"""Small, media-free contracts shared by Phase 3 adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PerceptionSource(StrEnum):
    CAMERA = "CAMERA"
    VIDEO = "VIDEO"
    STATIC_SYNTHETIC = "STATIC_SYNTHETIC"
    SYNTHETIC_IMAGE = "SYNTHETIC_IMAGE"
    DEMO_IMAGE = "DEMO_IMAGE"


class InteractionSource(StrEnum):
    STEPAUDIO_ASR = "STEPAUDIO_ASR"
    TEXT_FALLBACK = "TEXT_FALLBACK"


class ModelSource(StrEnum):
    STEP3 = "STEP3"
    RULE_FALLBACK = "RULE_FALLBACK"


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    component: str
    available: bool
    status: str
    latency_ms: int

    def as_dict(self) -> dict[str, str | bool | int]:
        return {
            "component": self.component,
            "available": self.available,
            "status": self.status,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True, slots=True)
class VisionObservation:
    person_present: bool
    source: PerceptionSource
    degraded: bool
    degraded_reasons: tuple[str, ...]
    latency_ms: int
    jpeg: bytes | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "person_present": self.person_present,
            "source": self.source.value,
            "degraded": self.degraded,
            "degraded_reasons": list(self.degraded_reasons),
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    text: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class TTSResult:
    status: str
    source: str
    audio_ref: str | None
    latency_ms: int

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "status": self.status,
            "source": self.source,
            "audio_ref": self.audio_ref,
            "latency_ms": self.latency_ms,
        }
