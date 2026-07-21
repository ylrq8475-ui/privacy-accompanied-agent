from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from backend.app.schemas.analysis import TextStateLabel
from backend.app.schemas.music import (
    PlaylistKey,
    logical_track_for_playlist,
    playlist_for_emotion,
)
from tests.phase1c.helpers import FixedClock, NOW
from tests.phase2.helpers import json_request
from track_catalog.api import CatalogASGIApp
from track_catalog.contracts import (
    CatalogLeaseRequest,
    CatalogResultRequest,
    CatalogSnapshotRequest,
)
from track_catalog.store import CatalogStore


def snapshot(
    playlist_key: PlaylistKey = PlaylistKey.RELAX,
    track_ids: list[str] | None = None,
) -> CatalogSnapshotRequest:
    values = track_ids or ["trackA", "trackB", "trackC"]
    return CatalogSnapshotRequest(
        playlist_key=playlist_key,
        playlist_id=f"playlist{playlist_key.value}",
        track_ids=values,
        source_count=len(values),
        truncated=False,
    )


def lease(action_id: str, playlist_key: PlaylistKey = PlaylistKey.RELAX):
    return CatalogLeaseRequest(
        action_id=action_id,
        playlist_key=playlist_key,
        logical_track_id=logical_track_for_playlist(playlist_key),
    )


class EmotionMappingTests(unittest.TestCase):
    def test_all_nine_labels_map_to_five_fixed_categories(self) -> None:
        expected = {
            TextStateLabel.PHYSICAL_FATIGUE: PlaylistKey.RELAX,
            TextStateLabel.STRESSED: PlaylistKey.RELAX,
            TextStateLabel.ANXIOUS: PlaylistKey.RELAX,
            TextStateLabel.EMOTIONAL_LOW: PlaylistKey.COMFORT,
            TextStateLabel.LONELY: PlaylistKey.COMFORT,
            TextStateLabel.HAPPY: PlaylistKey.UPLIFT,
            TextStateLabel.ANGRY: PlaylistKey.COOLDOWN,
            TextStateLabel.CALM: PlaylistKey.NEUTRAL,
            TextStateLabel.OTHER: PlaylistKey.NEUTRAL,
        }
        self.assertEqual(
            {label: playlist_for_emotion(label) for label in TextStateLabel},
            expected,
        )


class CatalogStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "catalog.sqlite3"
        self.clock = FixedClock()
        self.store = CatalogStore(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_robin_wrap_and_repeated_action_are_deterministic(self) -> None:
        stored = self.store.replace_snapshot(snapshot())
        self.assertEqual(stored.track_count, 3)
        first = self.store.lease(lease("music-action-001"))
        second = self.store.lease(lease("music-action-002"))
        repeated = self.store.lease(lease("music-action-001"))
        third = self.store.lease(lease("music-action-003"))
        wrapped = self.store.lease(lease("music-action-004"))
        self.assertEqual(
            [first.provider_track_id, second.provider_track_id, third.provider_track_id, wrapped.provider_track_id],
            ["trackA", "trackB", "trackC", "trackA"],
        )
        self.assertEqual(repeated.provider_track_id, "trackA")
        self.assertTrue(repeated.repeated)
        self.assertEqual(repeated.revision, stored.revision)

    def test_failed_track_is_skipped_and_successful_sync_resets_it(self) -> None:
        self.store.replace_snapshot(snapshot())
        first = self.store.lease(lease("music-action-001"))
        self.store.record_result(
            CatalogResultRequest(
                action_id=first.action_id,
                playlist_key=PlaylistKey.RELAX,
                provider_track_id=first.provider_track_id,
                outcome="FETCH_FAILED",
                reason_code="AUDIUS_TRACK_GATED",
            )
        )
        self.assertEqual(self.store.health()["categories"]["RELAX"]["ready_count"], 2)
        self.assertEqual(
            self.store.lease(lease("music-action-002")).provider_track_id,
            "trackB",
        )
        self.store.replace_snapshot(snapshot())
        self.assertEqual(self.store.health()["categories"]["RELAX"]["ready_count"], 3)

    def test_categories_are_isolated_and_restart_preserves_cursor_and_staleness(self) -> None:
        self.store.replace_snapshot(snapshot(PlaylistKey.RELAX, ["relaxA", "relaxB"]))
        self.store.replace_snapshot(snapshot(PlaylistKey.COMFORT, ["comfortA", "comfortB"]))
        self.assertEqual(
            self.store.lease(lease("music-relax-001", PlaylistKey.RELAX)).provider_track_id,
            "relaxA",
        )
        self.assertEqual(
            self.store.lease(lease("music-comfort-001", PlaylistKey.COMFORT)).provider_track_id,
            "comfortA",
        )
        restarted = CatalogStore(self.path, clock=self.clock)
        self.assertEqual(
            restarted.lease(lease("music-relax-002", PlaylistKey.RELAX)).provider_track_id,
            "relaxB",
        )
        self.clock.value = NOW + timedelta(hours=25)
        self.assertEqual(restarted.health()["categories"]["RELAX"]["status"], "STALE")

    def test_snapshot_contract_rejects_duplicates_mismatch_and_over_500(self) -> None:
        invalid = (
            {
                "playlist_key": "RELAX",
                "playlist_id": "playlist001",
                "track_ids": ["same", "same"],
                "source_count": 2,
                "truncated": False,
            },
            {
                "playlist_key": "RELAX",
                "playlist_id": "playlist001",
                "track_ids": [f"track{index}" for index in range(501)],
                "source_count": 501,
                "truncated": False,
            },
            {
                "playlist_key": "UPLIFT",
                "playlist_id": "playlist001",
                "track_ids": ["trackA"],
                "source_count": 2,
                "truncated": False,
            },
        )
        for value in invalid:
            with self.subTest(value=value["source_count"]):
                with self.assertRaises(ValidationError):
                    CatalogSnapshotRequest.model_validate(value)


class CatalogAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        store = CatalogStore(Path(self.temporary.name) / "api.sqlite3", clock=FixedClock())
        self.app = CatalogASGIApp(store)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_strict_local_api_and_health(self) -> None:
        status, body = await json_request(self.app, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["network_scope"], "LOCAL")
        status, body = await json_request(
            self.app,
            "PUT",
            "/v1/catalog/snapshot",
            snapshot().model_dump(mode="json"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["track_count"], 3)
        invalid = snapshot().model_dump(mode="json")
        invalid["credential"] = "synthetic-forbidden"
        status, body = await json_request(
            self.app, "PUT", "/v1/catalog/snapshot", invalid
        )
        self.assertEqual((status, body["error"]), (422, "SCHEMA_REJECTED"))
        status, body = await json_request(
            self.app, "POST", "/v1/catalog/lease", b"x" * 65_537
        )
        self.assertEqual((status, body["error"]), (400, "INVALID_REQUEST"))

    def test_catalog_package_has_no_internet_transport(self) -> None:
        root = Path(__file__).parents[2] / "track_catalog"
        source = "\n".join(
            path.read_text(encoding="utf-8") for path in root.glob("*.py")
        )
        for forbidden in ("urllib", "http.client", "requests", "socket"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
