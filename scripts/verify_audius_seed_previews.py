"""Verify every active Audius seed through the external connector.

Returned audio is validated in memory and discarded. This proves provider
fetch and decoder success, not that a person heard a physical speaker.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SEED = Path(
    "data/music/seeds/audius_mood_playlist_20_tracks.resolved.json"
)
LOGICAL_TRACKS = {
    "RELAX": "emotion_relax_01",
    "COMFORT": "emotion_comfort_01",
    "UPLIFT": "emotion_uplift_01",
    "COOLDOWN": "emotion_cooldown_01",
    "NEUTRAL": "emotion_neutral_01",
}
MAX_AUDIO_BYTES = 8 * 1024 * 1024


def _request_body(index: int, logical_track_id: str) -> bytes:
    return json.dumps(
        {
            "request_id": f"seed-preview-{index:03d}",
            "source_agent": "music-agent",
            "destination": "PUBLIC_MUSIC_API",
            "network_scope": "INTERNET",
            "payload": {"action": "play", "track_id": logical_track_id},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    ).encode("utf-8")


def _percentile_95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def verify(
    *, base_url: str, seed_path: Path, timeout_seconds: float
) -> dict[str, Any]:
    document = json.loads(seed_path.read_text(encoding="utf-8"))
    tracks = document.get("tracks")
    if not isinstance(tracks, list) or len(tracks) != 13:
        raise ValueError("active seed catalog must contain exactly 13 tracks")

    results: list[dict[str, Any]] = []
    for index, track in enumerate(tracks, start=1):
        provider_track_id = str(track["track_id"])
        playlist_keys = track.get("playlist_keys")
        if not isinstance(playlist_keys, list) or not playlist_keys:
            raise ValueError(f"seed track {provider_track_id} has no playlist key")
        logical_track_id = LOGICAL_TRACKS[str(playlist_keys[0])]
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/audius/tracks/{provider_track_id}/preview",
            data=_request_body(index, logical_track_id),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                audio = response.read(MAX_AUDIO_BYTES + 1)
                content_type = response.headers.get_content_type()
                returned_id = response.headers.get("X-Provider-Track-Id", "")
                if (
                    response.status != 200
                    or not content_type.startswith("audio/")
                    or returned_id != provider_track_id
                    or not audio
                    or len(audio) > MAX_AUDIO_BYTES
                ):
                    raise ValueError("preview response validation failed")
                result = {
                    "track_id": provider_track_id,
                    "status": "SUCCEEDED",
                    "size_bytes": len(audio),
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                }
        except (OSError, ValueError, urllib.error.HTTPError) as error:
            result = {
                "track_id": provider_track_id,
                "status": "FAILED",
                "error_type": type(error).__name__,
                "latency_ms": round((time.perf_counter() - started) * 1000),
            }
        results.append(result)

    successes = [item for item in results if item["status"] == "SUCCEEDED"]
    latencies = [int(item["latency_ms"]) for item in successes]
    return {
        "component": "AUDIUS_ACTIVE_SEED_PREVIEW",
        "provider": "AUDIUS",
        "network_scope": "INTERNET",
        "audio_persisted": False,
        "physical_speaker_verified": False,
        "sample_count": len(results),
        "success_count": len(successes),
        "failure_count": len(results) - len(successes),
        "p95_latency_ms": _percentile_95(latencies),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8030")
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = verify(
        base_url=args.base_url,
        seed_path=args.seed,
        timeout_seconds=args.timeout,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
