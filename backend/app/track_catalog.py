"""Fixed-loopback LOCAL client for the independent Track Catalog process."""

from __future__ import annotations

import os
from typing import Protocol

from backend.app.adapters import AdapterError
from backend.app.adapters.transport import JsonTransport, LocalJSONTransport
from backend.app.schemas.music import PlaylistKey
from track_catalog.contracts import (
    CatalogLeaseRequest,
    CatalogLeaseResponse,
    CatalogResultRequest,
    CatalogSnapshotRequest,
    CatalogSnapshotResponse,
)


CATALOG_BASE_URL = "http://127.0.0.1:8011"
CATALOG_TIMEOUT_SECONDS = 1.0
CATALOG_MAX_RESPONSE_BYTES = 65_536


class TrackCatalogError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TrackCatalog(Protocol):
    def health(self) -> dict[str, object]: ...
    def public_catalog(self) -> dict[str, object]: ...
    def category_status(self, playlist_key: PlaylistKey) -> str: ...
    def replace_snapshot(self, request: CatalogSnapshotRequest) -> CatalogSnapshotResponse: ...
    def lease(self, request: CatalogLeaseRequest) -> CatalogLeaseResponse: ...
    def record_result(self, request: CatalogResultRequest) -> None: ...


class TrackCatalogClient:
    def __init__(
        self,
        *,
        transport: JsonTransport | None = None,
        base_url: str | None = None,
    ) -> None:
        self.transport = transport or LocalJSONTransport()
        self.base_url = base_url or os.environ.get(
            "SPARK_TRACK_CATALOG_URL", CATALOG_BASE_URL
        )

    def health(self) -> dict[str, object]:
        return self._request("GET", "/health", None)

    def public_catalog(self) -> dict[str, object]:
        return self._request("GET", "/v1/catalog/public", None)

    def category_status(self, playlist_key: PlaylistKey) -> str:
        health = self.health()
        categories = health.get("categories")
        if not isinstance(categories, dict):
            raise TrackCatalogError("CATALOG_HEALTH_REJECTED")
        category = categories.get(playlist_key.value)
        if not isinstance(category, dict) or not isinstance(category.get("status"), str):
            raise TrackCatalogError("CATALOG_HEALTH_REJECTED")
        return str(category["status"])

    def replace_snapshot(
        self, request: CatalogSnapshotRequest
    ) -> CatalogSnapshotResponse:
        body = self._request(
            "PUT", "/v1/catalog/snapshot", request.model_dump(mode="json")
        )
        return CatalogSnapshotResponse.model_validate(body)

    def lease(self, request: CatalogLeaseRequest) -> CatalogLeaseResponse:
        body = self._request(
            "POST", "/v1/catalog/lease", request.model_dump(mode="json")
        )
        return CatalogLeaseResponse.model_validate(body)

    def record_result(self, request: CatalogResultRequest) -> None:
        body = self._request(
            "POST", "/v1/catalog/result", request.model_dump(mode="json")
        )
        if body != {"status": "RECORDED"}:
            raise TrackCatalogError("CATALOG_RESULT_REJECTED")

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> dict[str, object]:
        try:
            response = self.transport.request(
                method,
                self.base_url,
                path,
                body,
                timeout_seconds=CATALOG_TIMEOUT_SECONDS,
                max_response_bytes=CATALOG_MAX_RESPONSE_BYTES,
            )
        except AdapterError as error:
            raise TrackCatalogError("CATALOG_UNAVAILABLE") from error
        if response.status != 200:
            code = response.body.get("error")
            raise TrackCatalogError(
                str(code) if isinstance(code, str) else "CATALOG_REQUEST_FAILED"
            )
        return response.body
