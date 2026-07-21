"""SQLite implementation for the LOCAL-only bounded Track Catalog."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Callable

from backend.app.schemas.music import PlaylistKey

from .contracts import (
    CatalogLeaseRequest,
    CatalogLeaseResponse,
    CatalogResultRequest,
    CatalogSnapshotRequest,
    CatalogSnapshotResponse,
)


DEFAULT_CATALOG_PATH = Path("data/audius_catalog.sqlite3")
CATALOG_TTL = timedelta(hours=24)
Clock = Callable[[], datetime]


class CatalogError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS catalog_meta (
    playlist_key TEXT PRIMARY KEY CHECK (
        playlist_key IN ('RELAX', 'COMFORT', 'UPLIFT', 'COOLDOWN', 'NEUTRAL')
    ),
    playlist_id TEXT NOT NULL,
    revision TEXT NOT NULL CHECK (length(revision) = 64),
    synced_at TEXT NOT NULL,
    source_count INTEGER NOT NULL CHECK (source_count >= 1),
    truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)),
    last_selected_track_id TEXT
);
CREATE TABLE IF NOT EXISTS tracks (
    playlist_key TEXT NOT NULL,
    track_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0 AND ordinal < 500),
    state TEXT NOT NULL CHECK (state IN ('READY', 'DEGRADED')),
    failure_reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (playlist_key, track_id),
    UNIQUE (playlist_key, ordinal),
    FOREIGN KEY (playlist_key) REFERENCES catalog_meta(playlist_key) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS leases (
    action_id TEXT PRIMARY KEY,
    playlist_key TEXT NOT NULL,
    logical_track_id TEXT NOT NULL,
    provider_track_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    revision TEXT NOT NULL,
    leased_at TEXT NOT NULL,
    outcome TEXT CHECK (outcome IN ('FETCH_SUCCEEDED', 'FETCH_FAILED'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_ready
ON tracks(playlist_key, state, ordinal);
CREATE INDEX IF NOT EXISTS idx_leases_time
ON leases(leased_at, action_id);
"""


class CatalogStore:
    def __init__(
        self,
        path: str | Path = DEFAULT_CATALOG_PATH,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.executescript(_SCHEMA)
                connection.commit()
            self._initialized = True

    def replace_snapshot(
        self, request: CatalogSnapshotRequest
    ) -> CatalogSnapshotResponse:
        self.initialize()
        now = self._now()
        key = request.playlist_key.value
        revision = hashlib.sha256(
            json.dumps(
                request.track_ids,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = connection.execute(
                    "SELECT last_selected_track_id FROM catalog_meta WHERE playlist_key=?",
                    (key,),
                ).fetchone()
                last_selected = previous[0] if previous is not None else None
                if last_selected not in request.track_ids:
                    last_selected = None
                connection.execute(
                    """
                    INSERT INTO catalog_meta(
                        playlist_key, playlist_id, revision, synced_at,
                        source_count, truncated, last_selected_track_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(playlist_key) DO UPDATE SET
                        playlist_id=excluded.playlist_id,
                        revision=excluded.revision,
                        synced_at=excluded.synced_at,
                        source_count=excluded.source_count,
                        truncated=excluded.truncated,
                        last_selected_track_id=excluded.last_selected_track_id
                    """,
                    (
                        key,
                        request.playlist_id,
                        revision,
                        now.isoformat(),
                        request.source_count,
                        int(request.truncated),
                        last_selected,
                    ),
                )
                connection.execute("DELETE FROM tracks WHERE playlist_key=?", (key,))
                connection.executemany(
                    """
                    INSERT INTO tracks(
                        playlist_key, track_id, ordinal, state, failure_reason, updated_at
                    ) VALUES (?, ?, ?, 'READY', NULL, ?)
                    """,
                    [
                        (key, track_id, ordinal, now.isoformat())
                        for ordinal, track_id in enumerate(request.track_ids)
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return CatalogSnapshotResponse(
            playlist_key=request.playlist_key,
            playlist_id=request.playlist_id,
            revision=revision,
            track_count=len(request.track_ids),
            source_count=request.source_count,
            truncated=request.truncated,
            synced_at=now,
        )

    def lease(self, request: CatalogLeaseRequest) -> CatalogLeaseResponse:
        self.initialize()
        now = self._now()
        key = request.playlist_key.value
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM leases WHERE action_id=?", (request.action_id,)
                ).fetchone()
                if existing is not None:
                    if (
                        existing[1] != key
                        or existing[2] != request.logical_track_id
                    ):
                        raise CatalogError("CATALOG_ACTION_BINDING_MISMATCH")
                    meta = connection.execute(
                        "SELECT synced_at FROM catalog_meta WHERE playlist_key=?",
                        (key,),
                    ).fetchone()
                    stale = True if meta is None else self._is_stale(meta[0], now)
                    connection.commit()
                    return CatalogLeaseResponse(
                        action_id=existing[0],
                        playlist_key=existing[1],
                        logical_track_id=existing[2],
                        provider_track_id=existing[3],
                        ordinal=existing[4],
                        revision=existing[5],
                        stale=stale,
                        repeated=True,
                    )

                meta = connection.execute(
                    "SELECT revision, synced_at, last_selected_track_id "
                    "FROM catalog_meta WHERE playlist_key=?",
                    (key,),
                ).fetchone()
                if meta is None:
                    raise CatalogError("CATALOG_EMPTY")
                last_ordinal = -1
                if meta[2] is not None:
                    last = connection.execute(
                        "SELECT ordinal FROM tracks WHERE playlist_key=? AND track_id=?",
                        (key, meta[2]),
                    ).fetchone()
                    if last is not None:
                        last_ordinal = int(last[0])
                track = connection.execute(
                    """
                    SELECT track_id, ordinal FROM tracks
                    WHERE playlist_key=? AND state='READY' AND ordinal>?
                    ORDER BY ordinal LIMIT 1
                    """,
                    (key, last_ordinal),
                ).fetchone()
                if track is None:
                    track = connection.execute(
                        """
                        SELECT track_id, ordinal FROM tracks
                        WHERE playlist_key=? AND state='READY'
                        ORDER BY ordinal LIMIT 1
                        """,
                        (key,),
                    ).fetchone()
                if track is None:
                    raise CatalogError("CATALOG_NO_READY_TRACKS")
                connection.execute(
                    "UPDATE catalog_meta SET last_selected_track_id=? WHERE playlist_key=?",
                    (track[0], key),
                )
                connection.execute(
                    """
                    INSERT INTO leases(
                        action_id, playlist_key, logical_track_id, provider_track_id,
                        ordinal, revision, leased_at, outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        request.action_id,
                        key,
                        request.logical_track_id,
                        track[0],
                        track[1],
                        meta[0],
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM leases WHERE action_id IN (
                        SELECT action_id FROM leases
                        ORDER BY leased_at DESC, action_id DESC
                        LIMIT -1 OFFSET 1000
                    )
                    """
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return CatalogLeaseResponse(
            action_id=request.action_id,
            playlist_key=request.playlist_key,
            logical_track_id=request.logical_track_id,
            provider_track_id=str(track[0]),
            ordinal=int(track[1]),
            revision=str(meta[0]),
            stale=self._is_stale(str(meta[1]), now),
            repeated=False,
        )

    def record_result(self, request: CatalogResultRequest) -> None:
        self.initialize()
        now = self._now()
        key = request.playlist_key.value
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                lease = connection.execute(
                    "SELECT playlist_key, provider_track_id FROM leases WHERE action_id=?",
                    (request.action_id,),
                ).fetchone()
                if lease is None:
                    raise CatalogError("CATALOG_LEASE_NOT_FOUND")
                if lease[0] != key or lease[1] != request.provider_track_id:
                    raise CatalogError("CATALOG_RESULT_BINDING_MISMATCH")
                connection.execute(
                    "UPDATE leases SET outcome=? WHERE action_id=?",
                    (request.outcome, request.action_id),
                )
                if request.outcome == "FETCH_FAILED":
                    connection.execute(
                        """
                        UPDATE tracks SET state='DEGRADED', failure_reason=?, updated_at=?
                        WHERE playlist_key=? AND track_id=?
                        """,
                        (
                            request.reason_code,
                            now.isoformat(),
                            key,
                            request.provider_track_id,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def health(self) -> dict[str, object]:
        self.initialize()
        now = self._now()
        categories: dict[str, dict[str, object]] = {}
        with self._lock, closing(self._connect()) as connection:
            for playlist_key in PlaylistKey:
                meta = connection.execute(
                    """
                    SELECT playlist_id, revision, synced_at, source_count, truncated
                    FROM catalog_meta WHERE playlist_key=?
                    """,
                    (playlist_key.value,),
                ).fetchone()
                counts = connection.execute(
                    """
                    SELECT COUNT(*), SUM(CASE WHEN state='READY' THEN 1 ELSE 0 END)
                    FROM tracks WHERE playlist_key=?
                    """,
                    (playlist_key.value,),
                ).fetchone()
                total = int(counts[0] or 0)
                ready = int(counts[1] or 0)
                if meta is None or total == 0:
                    status = "EMPTY"
                elif ready == 0:
                    status = "DEGRADED"
                elif self._is_stale(str(meta[2]), now):
                    status = "STALE"
                else:
                    status = "READY"
                categories[playlist_key.value] = {
                    "status": status,
                    "track_count": total,
                    "ready_count": ready,
                    "playlist_id": None if meta is None else meta[0],
                    "revision": None if meta is None else meta[1],
                    "synced_at": None if meta is None else meta[2],
                    "source_count": 0 if meta is None else meta[3],
                    "truncated": False if meta is None else bool(meta[4]),
                }
        statuses = {item["status"] for item in categories.values()}
        overall = "READY" if statuses == {"READY"} else "PARTIAL"
        if statuses == {"EMPTY"}:
            overall = "EMPTY"
        elif "DEGRADED" in statuses:
            overall = "DEGRADED"
        elif "STALE" in statuses:
            overall = "STALE"
        return {
            "component": "AUDIUS_CATALOG",
            "available": True,
            "status": overall,
            "network_scope": "LOCAL",
            "categories": categories,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("catalog clock must be timezone-aware")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _is_stale(raw: str, now: datetime) -> bool:
        return now - datetime.fromisoformat(raw) >= CATALOG_TTL
