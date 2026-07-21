from __future__ import annotations

import json
from pathlib import Path

from external_connector.weather import ExternalConnectorTransportError


REAL_WEATHER_BODY = json.dumps(
    {"current": {"temperature_2m": 25.5, "weather_code": 1}}
).encode("utf-8")


class FakeWeatherTransport:
    def __init__(self, body: bytes = REAL_WEATHER_BODY, *, fail: bool = False) -> None:
        self.body = body
        self.fail = fail
        self.calls: list[tuple[str, float]] = []

    def fetch(self, url: str, timeout_seconds: float) -> bytes:
        self.calls.append((url, timeout_seconds))
        if self.fail:
            raise ExternalConnectorTransportError("synthetic weather outage")
        return self.body


class RecordingPlaybackBackend:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.paths: list[Path] = []
        self.memory_payloads: list[bytes] = []
        self.closed = 0

    def play(self, path: Path) -> None:
        if self.fail:
            raise RuntimeError("synthetic audio device failure")
        self.paths.append(path)

    def play_memory(self, audio: bytes) -> None:
        if self.fail:
            raise RuntimeError("synthetic audio device failure")
        self.memory_payloads.append(audio)

    def close(self) -> None:
        self.closed += 1
