from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DeploymentArtifactTests(unittest.TestCase):
    def test_compose_keeps_backend_off_egress_and_unpublished(self) -> None:
        compose = (ROOT / "docker-compose.dgx.yml").read_text(encoding="utf-8")
        backend = compose.split("  backend:", 1)[1].split("\nnetworks:", 1)[0]
        connector = compose.split("  external-connector:", 1)[1].split(
            "\n  track-catalog:", 1
        )[0]
        self.assertNotIn("ports:", backend)
        self.assertIn("- companion-private", backend)
        self.assertIn("- demo-private", backend)
        self.assertNotIn("- demo-egress", backend)
        self.assertIn("- demo-egress", connector)
        self.assertIn("demo-private:\n    internal: true", compose)
        self.assertIn("companion-private:\n    external: true", compose)

    def test_deployment_archive_uses_allowlist_and_rejects_private_state(self) -> None:
        script = (ROOT / "scripts" / "deploy_dgx_spark.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$SafeDirectories", script)
        self.assertIn("$SafeFiles", script)
        self.assertIn("audius_playlists\\.local\\.json", script)
        self.assertIn('"docker-compose.dgx.audius-sync.yml"', script)
        self.assertIn('Filter "*.sh"', script)
        self.assertIn('.Replace("`r`n", "`n")', script)
        self.assertIn("\\.sqlite3", script)
        self.assertNotIn('"data/demo.sqlite3"', script)
        self.assertNotIn("docker compose down", script)
        tunnel = (ROOT / "scripts" / "start_dgx_console_tunnel.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("${PrivateNetwork}/backend:8000", tunnel)
        self.assertNotIn("127.0.0.1:${RemotePort}", tunnel)

    def test_runtime_image_is_non_root_and_test_stage_has_no_default_network_need(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        remote = (ROOT / "scripts" / "deploy_remote_dgx.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("FROM nvcr.io/nvidia/vllm:26.06-py3 AS python-base", dockerfile)
        self.assertIn("COPY console/dist ./console/dist", dockerfile)
        self.assertIn("miniaudio==1.71", dockerfile)
        self.assertNotIn("COPY --from=console-build", dockerfile)
        self.assertIn("FROM python-base AS test", dockerfile)
        self.assertIn("COPY Dockerfile docker-compose.dgx.yml ./", dockerfile)
        self.assertIn("USER spark-demo", dockerfile)
        self.assertIn('docker run --rm --network none "${image_tag}-test"', remote)
        self.assertIn("docker-compose.dgx.audius.yml", remote)
        rollback = (ROOT / "scripts" / "rollback_remote_dgx.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("docker-compose.dgx.audius.yml", rollback)
        self.assertNotIn("sudo", remote)
        self.assertNotIn("docker compose down", remote)
