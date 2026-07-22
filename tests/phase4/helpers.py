from __future__ import annotations

import hashlib
import json
from pathlib import Path

from external_connector.weather import ExternalConnectorTransportError


REAL_WEATHER_BODY = json.dumps(
    {"current": {"temperature_2m": 25.5, "weather_code": 1}}
).encode("utf-8")


def make_synthetic_music_root(root: Path) -> Path:
    """Create a non-played test fixture without shipping unlicensed audio."""
    music_root = root / "music"
    track_path = music_root / "tracks" / "calm_piano_01.flac"
    track_path.parent.mkdir(parents=True, exist_ok=True)
    track_path.write_bytes(b"synthetic-test-audio-not-for-playback")
    (music_root / "catalog.json").write_text(
        json.dumps(
            {
                "tracks": [
                    {
                        "track_id": "calm_piano_01",
                        "path": "tracks/calm_piano_01.flac",
                        "sha256": hashlib.sha256(track_path.read_bytes()).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return music_root


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
