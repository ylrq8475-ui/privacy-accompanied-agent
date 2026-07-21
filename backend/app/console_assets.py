"""Allowlisted static asset resolution for the built Phase 2 console."""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONSOLE_DIRECTORY = Path(__file__).resolve().parents[2] / "console" / "dist"


@dataclass(frozen=True, slots=True)
class ConsoleAssetResponse:
    status: int
    body: bytes
    content_type: str
    cache_control: str = "no-store"


class ConsoleAssets:
    """Resolves only files beneath the configured Vite output directory."""

    def __init__(self, directory: str | Path = DEFAULT_CONSOLE_DIRECTORY) -> None:
        self.directory = Path(directory).resolve()

    def resolve(self, path: str) -> ConsoleAssetResponse:
        relative_path = "index.html" if path == "/console/" else path.removeprefix(
            "/console/"
        )
        if not self.directory.is_dir():
            return self._json_error(503, "CONSOLE_NOT_BUILT")
        if not relative_path or relative_path.endswith("/"):
            return self._json_error(404, "CONSOLE_ASSET_NOT_FOUND")

        candidate = (self.directory / relative_path).resolve()
        try:
            candidate.relative_to(self.directory)
        except ValueError:
            return self._json_error(404, "CONSOLE_ASSET_NOT_FOUND")
        if not candidate.is_file():
            return self._json_error(404, "CONSOLE_ASSET_NOT_FOUND")

        guessed_type, _ = mimetypes.guess_type(candidate.name)
        content_type = guessed_type or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type = f"{content_type}; charset=utf-8"
        cache_control = (
            "public, max-age=31536000, immutable"
            if relative_path.startswith("assets/")
            else "no-store"
        )
        return ConsoleAssetResponse(
            status=200,
            body=candidate.read_bytes(),
            content_type=content_type,
            cache_control=cache_control,
        )

    @staticmethod
    def _json_error(status: int, error: str) -> ConsoleAssetResponse:
        body = json.dumps({"error": error}, separators=(",", ":")).encode("utf-8")
        return ConsoleAssetResponse(
            status=status,
            body=body,
            content_type="application/json; charset=utf-8",
        )
