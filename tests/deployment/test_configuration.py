from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.api import DemoASGIApp, _default_orchestrator
from backend.app.orchestrator import Orchestrator
from external_connector.audius import AudiusSettings
from external_connector.client import RemoteAudiusConnector, RemoteWeatherConnector
from tests.phase1b.test_api import http_request


class DeploymentConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_reports_non_secret_deployment_identity(self) -> None:
        app = DemoASGIApp(Orchestrator())
        with patch.dict(
            "os.environ",
            {
                "SPARK_DEPLOYMENT_TARGET": "DGX_SPARK",
                "SPARK_CONSOLE_ACCESS": "SSH_LOOPBACK",
            },
            clear=False,
        ):
            status, body = await http_request(app, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            body["deployment"],
            {"backend": "DGX_SPARK", "console_access": "SSH_LOOPBACK"},
        )

    def test_dgx_backend_refuses_direct_egress_configuration(self) -> None:
        with patch.dict(
            "os.environ",
            {"SPARK_DEPLOYMENT_TARGET": "DGX_SPARK", "SPARK_EXTERNAL_CONNECTOR_URL": ""},
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                _default_orchestrator()

    def test_dgx_backend_builds_only_remote_connector_clients(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                "os.environ",
                {
                    "SPARK_DEPLOYMENT_TARGET": "DGX_SPARK",
                    "SPARK_EXTERNAL_CONNECTOR_URL": "http://external-connector:8030",
                    "SPARK_TRACK_CATALOG_URL": "http://track-catalog:8011",
                    "SPARK_DATABASE_PATH": str(Path(temporary) / "demo.sqlite3"),
                },
                clear=False,
            ):
                orchestrator = _default_orchestrator()
            try:
                self.assertIsInstance(orchestrator.live_connector, RemoteWeatherConnector)
                self.assertIsInstance(orchestrator.live_audius, RemoteAudiusConnector)
            finally:
                orchestrator.close()


class AudiusSecretFileTests(unittest.TestCase):
    def test_connector_reads_secret_files_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api_key = root / "api_key"
            bearer = root / "bearer"
            playlists = root / "playlists.json"
            api_key.write_text("synthetic-api-key", encoding="utf-8")
            bearer.write_text("synthetic-bearer", encoding="utf-8")
            playlists.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "playlists": {"RELAX": "https://audius.co/demo/relax"},
                    }
                ),
                encoding="utf-8",
            )
            settings = AudiusSettings.from_environment(
                {
                    "SPARK_AUDIUS_ENABLED": "true",
                    "SPARK_AUDIUS_API_KEY_FILE": str(api_key),
                    "SPARK_AUDIUS_BEARER_TOKEN_FILE": str(bearer),
                    "SPARK_AUDIUS_PLAYLIST_CONFIG": str(playlists),
                }
            )
            self.assertTrue(settings.configured)
            self.assertNotIn("synthetic-api-key", repr(settings))
            self.assertNotIn("synthetic-bearer", repr(settings))

    def test_ambiguous_direct_and_file_secret_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret = Path(temporary) / "secret"
            secret.write_text("synthetic-file-value", encoding="utf-8")
            settings = AudiusSettings.from_environment(
                {
                    "SPARK_AUDIUS_ENABLED": "true",
                    "SPARK_AUDIUS_API_KEY": "synthetic-direct-value",
                    "SPARK_AUDIUS_API_KEY_FILE": str(secret),
                    "SPARK_AUDIUS_BEARER_TOKEN": "synthetic-bearer",
                }
            )
            self.assertFalse(settings.configured)
