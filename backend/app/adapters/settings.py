"""Non-secret Phase 3 runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEMO_INPUT_DIRECTORY = Path(__file__).resolve().parents[3] / "data" / "demo_inputs"


@dataclass(frozen=True, slots=True)
class Phase3Settings:
    stepaudio_url: str = "http://stepaudio:8010"
    step3_url: str = "http://step3-vl:8000"
    stepaudio_filename: str | None = None
    camera_index: int = 0
    video_path: Path | None = None

    @classmethod
    def from_environment(cls) -> "Phase3Settings":
        raw_video = os.environ.get("SPARK_DEMO_VIDEO_PATH")
        video_path = cls._safe_video_path(raw_video) if raw_video else None
        raw_camera = os.environ.get("SPARK_CAMERA_INDEX", "0")
        try:
            camera_index = int(raw_camera)
        except ValueError:
            camera_index = 0
        return cls(
            stepaudio_url=os.environ.get(
                "SPARK_STEPAUDIO_URL", "http://stepaudio:8010"
            ),
            step3_url=os.environ.get("SPARK_STEP3_URL", "http://step3-vl:8000"),
            stepaudio_filename=os.environ.get("SPARK_STEPAUDIO_FILENAME") or None,
            camera_index=max(camera_index, 0),
            video_path=video_path,
        )

    @staticmethod
    def _safe_video_path(value: str) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = DEMO_INPUT_DIRECTORY / candidate
        resolved = candidate.resolve()
        root = DEMO_INPUT_DIRECTORY.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("Demo video must be beneath data/demo_inputs") from error
        return resolved
