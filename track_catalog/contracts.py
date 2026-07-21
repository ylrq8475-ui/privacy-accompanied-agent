"""Strict LOCAL API contracts for the bounded Track Catalog."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from backend.app.schemas.base import Identifier, StrictModel, require_aware_datetime
from backend.app.schemas.music import PlaylistKeyValue, playlist_for_logical_track


ProviderId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9]{1,64}$"),
]
ReasonCode = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,95}$"),
]


class CatalogSnapshotRequest(StrictModel):
    playlist_key: PlaylistKeyValue
    playlist_id: ProviderId
    track_ids: list[ProviderId] = Field(min_length=1, max_length=500)
    source_count: int = Field(ge=1, le=100_000)
    truncated: bool

    @model_validator(mode="after")
    def validate_snapshot(self) -> "CatalogSnapshotRequest":
        if len(self.track_ids) != len(set(self.track_ids)):
            raise ValueError("track_ids must already be de-duplicated")
        if self.source_count < len(self.track_ids):
            raise ValueError("source_count cannot be smaller than stored track count")
        if self.truncated != (self.source_count > len(self.track_ids)):
            raise ValueError("truncated flag does not match source_count")
        return self


class CatalogSnapshotResponse(StrictModel):
    playlist_key: PlaylistKeyValue
    playlist_id: ProviderId
    revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    track_count: int = Field(ge=1, le=500)
    source_count: int = Field(ge=1)
    truncated: bool
    synced_at: datetime

    @model_validator(mode="after")
    def validate_time(self) -> "CatalogSnapshotResponse":
        require_aware_datetime(self.synced_at, "synced_at")
        return self


class CatalogLeaseRequest(StrictModel):
    action_id: Identifier
    playlist_key: PlaylistKeyValue
    logical_track_id: Identifier

    @model_validator(mode="after")
    def validate_binding(self) -> "CatalogLeaseRequest":
        if playlist_for_logical_track(self.logical_track_id) is not self.playlist_key:
            raise ValueError("logical_track_id is not bound to playlist_key")
        return self


class CatalogLeaseResponse(StrictModel):
    action_id: Identifier
    playlist_key: PlaylistKeyValue
    logical_track_id: Identifier
    provider_track_id: ProviderId
    ordinal: int = Field(ge=0, lt=500)
    revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    stale: bool
    repeated: bool


class CatalogResultRequest(StrictModel):
    action_id: Identifier
    playlist_key: PlaylistKeyValue
    provider_track_id: ProviderId
    outcome: Literal["FETCH_SUCCEEDED", "FETCH_FAILED"]
    reason_code: ReasonCode | None = None

    @model_validator(mode="after")
    def validate_reason(self) -> "CatalogResultRequest":
        if self.outcome == "FETCH_FAILED" and self.reason_code is None:
            raise ValueError("failed fetch requires a reason_code")
        if self.outcome == "FETCH_SUCCEEDED" and self.reason_code is not None:
            raise ValueError("successful fetch cannot include a reason_code")
        return self
