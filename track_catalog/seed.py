"""Validated local seed catalog used by the competition Demo."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend.app.schemas.music import PlaylistKey

from .contracts import CatalogSnapshotRequest
from .store import CatalogStore


DEFAULT_SEED_PATH = Path(
    "data/music/seeds/audius_mood_playlist_20_tracks.resolved.json"
)
TRACK_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
PUBLIC_TRACK_FIELDS = (
    "catalog_id",
    "track_id",
    "title",
    "artist",
    "genre",
    "audius_mood",
    "energy",
    "vocal_type",
)


def load_seed_manifest(path: str | Path = DEFAULT_SEED_PATH) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("unsupported seed manifest")
    tracks = raw.get("tracks")
    pools = raw.get("playlist_pools")
    playback_pools = raw.get("playback_pools", pools)
    if (
        not isinstance(tracks, list)
        or not isinstance(pools, dict)
        or not isinstance(playback_pools, dict)
    ):
        raise ValueError("seed manifest is missing tracks or playlist pools")

    resolved: dict[str, dict[str, Any]] = {}
    for item in tracks:
        if not isinstance(item, dict):
            raise ValueError("seed track must be an object")
        track_id = item.get("track_id")
        metadata = item.get("provider_metadata")
        if (
            not isinstance(track_id, str)
            or not TRACK_ID.fullmatch(track_id)
            or track_id in resolved
            or item.get("track_id_resolution") != "resolved_from_permalink"
            or item.get("review_status") not in {"pending_listen", "approved"}
            or not isinstance(metadata, Mapping)
            or metadata.get("is_streamable") is not True
            or metadata.get("is_unlisted") is not False
            or metadata.get("is_stream_gated") is not False
        ):
            raise ValueError("seed track is not a validated public preview candidate")
        if any(not isinstance(item.get(field), str) for field in PUBLIC_TRACK_FIELDS):
            raise ValueError("seed track is missing public display metadata")
        resolved[track_id] = item

    expected_keys = {item.value for item in PlaylistKey}
    if set(pools) != expected_keys or set(playback_pools) != expected_keys:
        raise ValueError("seed manifest must contain exactly five playlist pools")
    for pool_name, candidate_pools in (
        ("display", pools),
        ("playback", playback_pools),
    ):
        for key in expected_keys:
            ids = candidate_pools[key]
            if (
                not isinstance(ids, list)
                or not ids
                or len(ids) != len(set(ids))
                or any(not isinstance(item, str) or item not in resolved for item in ids)
                or (pool_name == "playback" and not set(ids).issubset(pools[key]))
            ):
                raise ValueError(f"invalid {pool_name} seed playlist pool: {key}")
    return raw


def seed_store(
    store: CatalogStore,
    path: str | Path = DEFAULT_SEED_PATH,
) -> dict[str, object]:
    manifest = load_seed_manifest(path)
    health = store.health()
    categories = health.get("categories")
    if not isinstance(categories, dict):
        raise ValueError("catalog health is missing categories")

    updated: list[str] = []
    unchanged: list[str] = []
    pools = manifest.get("playback_pools", manifest["playlist_pools"])
    for playlist_key in PlaylistKey:
        track_ids = list(pools[playlist_key.value])
        expected_revision = hashlib.sha256(
            json.dumps(track_ids, ensure_ascii=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        current = categories.get(playlist_key.value)
        current_revision = (
            current.get("revision") if isinstance(current, dict) else None
        )
        if current_revision == expected_revision:
            unchanged.append(playlist_key.value)
            continue
        store.replace_snapshot(
            CatalogSnapshotRequest(
                playlist_key=playlist_key,
                playlist_id=f"Seed{playlist_key.value}V1",
                track_ids=track_ids,
                source_count=len(track_ids),
                truncated=False,
            )
        )
        updated.append(playlist_key.value)
    return {
        "status": "READY",
        "source": "BUNDLED_SEED",
        "resolved_track_count": len(manifest["tracks"]),
        "playback_verified_track_count": len(
            {
                track_id
                for track_ids in pools.values()
                for track_id in track_ids
            }
        ),
        "updated_categories": updated,
        "unchanged_categories": unchanged,
    }


def public_catalog(
    store: CatalogStore,
    path: str | Path = DEFAULT_SEED_PATH,
) -> dict[str, object]:
    manifest = load_seed_manifest(path)
    health = store.health()
    health_categories = health.get("categories")
    if not isinstance(health_categories, dict):
        raise ValueError("catalog health is missing categories")
    track_by_id = {item["track_id"]: item for item in manifest["tracks"]}
    categories: list[dict[str, object]] = []
    for playlist_key in PlaylistKey:
        category_health = health_categories.get(playlist_key.value)
        safe_health = category_health if isinstance(category_health, dict) else {}
        display_track_ids = manifest["playlist_pools"][playlist_key.value]
        categories.append(
            {
                "key": playlist_key.value,
                "status": str(safe_health.get("status", "EMPTY")),
                "track_count": len(display_track_ids),
                "ready_count": int(safe_health.get("ready_count", 0)),
                "tracks": [
                    {
                        field: track_by_id[track_id][field]
                        for field in PUBLIC_TRACK_FIELDS
                    }
                    for track_id in display_track_ids
                ],
            }
        )
    return {
        "source": "BUNDLED_SEED",
        "local_only": True,
        "provider_urls_exposed": False,
        "credentials_exposed": False,
        "categories": categories,
    }
