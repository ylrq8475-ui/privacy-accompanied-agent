from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.demo_media import SyntheticSceneCatalog


class DemoSceneCatalogTests(unittest.TestCase):
    def test_private_mode_uses_allowlisted_git_ignored_jpegs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            media_root = Path(temporary)
            private_root = media_root / "private"
            private_root.mkdir()
            expected = b"\xff\xd8synthetic-private-scene\xff\xd9"
            (private_root / "indoor_person.jpg").write_bytes(expected)
            (private_root / "indoor_empty.jpg").write_bytes(expected)

            with patch.dict(os.environ, {"SPARK_DEMO_SCENE_MEDIA": "private"}), patch(
                "backend.app.demo_media.scenes.MEDIA_ROOT", media_root
            ):
                catalog = SyntheticSceneCatalog()
                manifest = [scene.public_dict() for scene in catalog.list_scenes()]
                image, content_type = catalog.read_image("indoor_person")

            self.assertTrue(all(item["synthetic"] is False for item in manifest))
            self.assertEqual(content_type, "image/jpeg")
            self.assertEqual(image, expected)

    def test_invalid_media_mode_is_rejected(self) -> None:
        with patch.dict(os.environ, {"SPARK_DEMO_SCENE_MEDIA": "uploads"}):
            with self.assertRaisesRegex(ValueError, "synthetic or private"):
                SyntheticSceneCatalog()


if __name__ == "__main__":
    unittest.main()
