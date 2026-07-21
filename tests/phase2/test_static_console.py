from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.api import DemoASGIApp
from backend.app.console_assets import ConsoleAssets
from backend.app.orchestrator import Orchestrator
from tests.phase2.helpers import request


class StaticConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_redirect_and_built_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary) / "dist"
            assets = dist / "assets"
            assets.mkdir(parents=True)
            (dist / "index.html").write_text("<main>console</main>", encoding="utf-8")
            (assets / "app.js").write_text("export {};", encoding="utf-8")
            app = DemoASGIApp(Orchestrator(), ConsoleAssets(dist))

            status, headers, body = await request(app, "GET", "/console")
            self.assertEqual(status, 307)
            self.assertEqual(headers[b"location"], b"/console/")
            self.assertEqual(body, b"")

            status, headers, body = await request(app, "GET", "/console/")
            self.assertEqual(status, 200)
            self.assertIn(b"console", body)
            self.assertTrue(headers[b"content-type"].startswith(b"text/html"))
            self.assertEqual(headers[b"cache-control"], b"no-store")

            status, headers, body = await request(
                app, "GET", "/console/assets/app.js"
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, b"export {};")
            self.assertEqual(
                headers[b"cache-control"], b"public, max-age=31536000, immutable"
            )

    async def test_missing_build_and_path_traversal_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing"
            app = DemoASGIApp(Orchestrator(), ConsoleAssets(missing))
            status, _, body = await request(app, "GET", "/console/")
            self.assertEqual(status, 503)
            self.assertIn(b"CONSOLE_NOT_BUILT", body)

            dist = root / "dist"
            dist.mkdir()
            (root / "secret.txt").write_text("secret", encoding="utf-8")
            app = DemoASGIApp(Orchestrator(), ConsoleAssets(dist))
            status, _, body = await request(
                app, "GET", "/console/../secret.txt"
            )
            self.assertEqual(status, 404)
            self.assertNotIn(b"secret", body)


if __name__ == "__main__":
    unittest.main()
