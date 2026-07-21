from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PLAYLIST_KEYS = ("RELAX", "COMFORT", "UPLIFT", "COOLDOWN", "NEUTRAL")
TRACK_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("unsupported seed manifest")
    tracks = manifest.get("tracks")
    pools = manifest.get("playlist_pools")
    if not isinstance(tracks, list) or not isinstance(pools, dict):
        raise ValueError("manifest is missing tracks or playlist pools")

    resolved_ids = {
        item.get("track_id")
        for item in tracks
        if isinstance(item, dict)
        and item.get("track_id_resolution") == "resolved_from_permalink"
    }
    if set(pools) != set(PLAYLIST_KEYS):
        raise ValueError("manifest must contain exactly five playlist pools")
    for key in PLAYLIST_KEYS:
        ids = pools[key]
        if not isinstance(ids, list) or not ids:
            raise ValueError(f"{key} pool must not be empty")
        if len(ids) != len(set(ids)):
            raise ValueError(f"{key} pool contains duplicate track IDs")
        if any(
            not isinstance(track_id, str)
            or not TRACK_ID.fullmatch(track_id)
            or track_id not in resolved_ids
            for track_id in ids
        ):
            raise ValueError(f"{key} pool contains an unresolved track ID")
    return manifest


def request_json(
    method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"catalog returned HTTP {error.code}: {detail}") from error
    if not isinstance(result, dict):
        raise ValueError("catalog response is not a JSON object")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--catalog-url", default="http://127.0.0.1:8011")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    pools = manifest["playlist_pools"]
    summary = {key: len(pools[key]) for key in PLAYLIST_KEYS}
    print(json.dumps({"validated": True, "pool_counts": summary}, indent=2))
    if not args.apply:
        print("dry-run only; pass --apply to update Track Catalog")
        return

    base_url = args.catalog_url.rstrip("/")
    before = request_json("GET", f"{base_url}/health")
    print(json.dumps({"before": before}, ensure_ascii=False, indent=2))
    for key in PLAYLIST_KEYS:
        track_ids = pools[key]
        payload = {
            "playlist_key": key,
            "playlist_id": f"Seed{key}V1",
            "track_ids": track_ids,
            "source_count": len(track_ids),
            "truncated": False,
        }
        result = request_json("PUT", f"{base_url}/v1/catalog/snapshot", payload)
        print(json.dumps({"seeded": result}, ensure_ascii=False))
    after = request_json("GET", f"{base_url}/health")
    print(json.dumps({"after": after}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
