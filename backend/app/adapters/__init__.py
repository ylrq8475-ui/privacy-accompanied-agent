"""Phase 3 adapters for LOCAL media and model services."""

from .audio import (
    MAX_TTS_WAV_BYTES,
    LocalWavTransport,
    StepAudioAdapter,
    WavTransportResponse,
)
from .contracts import (
    AdapterError,
    AdapterHealth,
    InteractionSource,
    ModelSource,
    PerceptionSource,
    TTSResult,
    VisionObservation,
)
from .settings import Phase3Settings
from .step3 import Step3Adapter, fixed_step3_fallback
from .transport import JsonTransport, LocalJSONTransport, TransportResponse
from .vision import VisionAdapter

__all__ = [
    "AdapterError",
    "AdapterHealth",
    "InteractionSource",
    "JsonTransport",
    "LocalJSONTransport",
    "LocalWavTransport",
    "MAX_TTS_WAV_BYTES",
    "ModelSource",
    "PerceptionSource",
    "Phase3Settings",
    "Step3Adapter",
    "StepAudioAdapter",
    "TTSResult",
    "TransportResponse",
    "VisionAdapter",
    "VisionObservation",
    "WavTransportResponse",
    "fixed_step3_fallback",
]
