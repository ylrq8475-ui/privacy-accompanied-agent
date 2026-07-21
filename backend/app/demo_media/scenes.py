"""Immutable catalog for the two approved Demo perception scenes."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from backend.app.adapters.contracts import AdapterError


MEDIA_ROOT = Path(__file__).resolve().parent
MAX_SCENE_BYTES = 524_288


@dataclass(frozen=True, slots=True)
class SyntheticScene:
    scene_id: str
    label: str
    filename: str
    content_type: str
    synthetic: bool

    @property
    def image_url(self) -> str:
        return f"/v1/live/perception/scenes/{self.scene_id}/image"

    def public_dict(self) -> dict[str, object]:
        return {
            "scene_id": self.scene_id,
            "label": self.label,
            "image_url": self.image_url,
            "synthetic": self.synthetic,
        }


class SyntheticSceneCatalog:
    def __init__(self) -> None:
        media_mode = os.getenv("SPARK_DEMO_SCENE_MEDIA", "synthetic")
        if media_mode not in {"synthetic", "private"}:
            raise ValueError("SPARK_DEMO_SCENE_MEDIA must be synthetic or private")
        if media_mode == "private":
            self._scenes = {
                "indoor_person": SyntheticScene(
                    "indoor_person",
                    "室内有人",
                    "private/indoor_person.jpg",
                    "image/jpeg",
                    False,
                ),
                "indoor_empty": SyntheticScene(
                    "indoor_empty",
                    "室内无人",
                    "private/indoor_empty.jpg",
                    "image/jpeg",
                    False,
                ),
            }
        else:
            self._scenes = {
                "indoor_person": SyntheticScene(
                    "indoor_person",
                    "室内有人",
                    "indoor_person.png",
                    "image/png",
                    True,
                ),
                "indoor_empty": SyntheticScene(
                    "indoor_empty",
                    "室内无人",
                    "indoor_empty.png",
                    "image/png",
                    True,
                ),
            }

    def list_scenes(self) -> list[SyntheticScene]:
        return list(self._scenes.values())

    def get(self, scene_id: str) -> SyntheticScene | None:
        return self._scenes.get(scene_id)

    def read_image(self, scene_id: str) -> tuple[bytes, str]:
        scene = self.get(scene_id)
        if scene is None:
            raise AdapterError("DEMO_SCENE_NOT_FOUND", "Demo scene is not allowlisted")
        path = (MEDIA_ROOT / scene.filename).resolve()
        try:
            path.relative_to(MEDIA_ROOT.resolve())
        except ValueError as error:
            raise AdapterError("DEMO_SCENE_REJECTED", "Demo scene escaped the catalog") from error
        try:
            data = path.read_bytes()
        except OSError as error:
            raise AdapterError("DEMO_SCENE_UNAVAILABLE", "Demo scene could not be read") from error
        valid_signature = (
            scene.content_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")
        ) or (
            scene.content_type == "image/jpeg"
            and data.startswith(b"\xff\xd8")
            and data.endswith(b"\xff\xd9")
        )
        if not valid_signature:
            raise AdapterError("DEMO_SCENE_INVALID", "Demo scene media signature is invalid")
        if len(data) > MAX_SCENE_BYTES:
            raise AdapterError("VISION_FRAME_TOO_LARGE", "Demo scene exceeded 512 KiB")
        return data, scene.content_type
