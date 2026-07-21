from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_RESOLVE = "https://api.audius.co/v1/resolve"
TRACK_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
PLAYLIST_KEYS = ("RELAX", "COMFORT", "UPLIFT", "COOLDOWN", "NEUTRAL")


def playlist_keys_for(track: dict[str, Any]) -> list[str]:
    contexts = set(track.get("contexts") or [])
    moods = set(track.get("moods") or [])
    keys: list[str] = []

    if contexts & {"physically_tired", "stressed", "anxious"}:
        keys.append("RELAX")
    if contexts & {"emotionally_low", "lonely"} or "healing" in moods:
        keys.append("COMFORT")
    if "happy" in contexts or moods & {"cheerful", "uplifting"}:
        keys.append("UPLIFT")
    if "angry" in contexts:
        keys.append("COOLDOWN")
    if contexts & {"calm", "focused"} or moods & {"focused", "reflective"}:
        keys.append("NEUTRAL")

    return [key for key in PLAYLIST_KEYS if key in keys] or ["NEUTRAL"]


def resolve(url: str, timeout_seconds: float) -> dict[str, Any]:
    query = urllib.parse.urlencode({"url": url})
    request = urllib.request.Request(
        f"{API_RESOLVE}?{query}",
        headers={"Accept": "application/json", "User-Agent": "Spark-Demo-Seed/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP_{response.status}")
        payload = json.load(response)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("resolve response has no track object")
    return data


def build(input_path: Path, output_path: Path, timeout_seconds: float) -> None:
    source = json.loads(input_path.read_text(encoding="utf-8-sig"))
    if not isinstance(source, list):
        raise ValueError("seed input must be a JSON array")

    resolved: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(source, 1):
        if not isinstance(raw, dict):
            failures.append({"catalog_id": f"INDEX-{index}", "reason": "INVALID_ENTRY"})
            continue
        item = dict(raw)
        catalog_id = str(item.get("catalog_id") or f"INDEX-{index}")
        try:
            metadata = resolve(str(item["audius_url"]), timeout_seconds)
            provider_id = metadata.get("id")
            if not isinstance(provider_id, str) or not TRACK_ID.fullmatch(provider_id):
                raise ValueError("resolved track id is invalid")
            if provider_id in seen_ids:
                raise ValueError("duplicate resolved track id")
            seen_ids.add(provider_id)
            if metadata.get("is_streamable") is not True:
                raise ValueError("track is not streamable")
            if metadata.get("is_unlisted") is True or metadata.get("is_stream_gated") is True:
                raise ValueError("track is not public and ungated")

            item["track_id"] = provider_id
            item["playlist_keys"] = playlist_keys_for(item)
            item["track_id_resolution"] = "resolved_from_permalink"
            item["provider_metadata"] = {
                "title": metadata.get("title"),
                "artist": (metadata.get("user") or {}).get("name"),
                "genre": metadata.get("genre"),
                "mood": metadata.get("mood"),
                "bpm": metadata.get("bpm"),
                "duration": metadata.get("duration"),
                "is_streamable": metadata.get("is_streamable"),
                "is_unlisted": metadata.get("is_unlisted"),
                "is_stream_gated": metadata.get("is_stream_gated"),
            }
            resolved.append(item)
        except Exception as error:
            failures.append({"catalog_id": catalog_id, "reason": type(error).__name__})
        time.sleep(0.1)

    pools = {
        key: [item["track_id"] for item in resolved if key in item["playlist_keys"]]
        for key in PLAYLIST_KEYS
    }
    output = {
        "version": 1,
        "purpose": "preliminary_audius_demo_seed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_count": len(source),
        "resolved_count": len(resolved),
        "failed_count": len(failures),
        "tracks": resolved,
        "playlist_pools": pools,
        "failures": failures,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    build(args.input, args.output, args.timeout)


if __name__ == "__main__":
    main()
