"""Verify the DGX Compose boundary without reading secrets or model configuration."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from typing import Any


def run(*command: str) -> str:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def service_id(compose_file: str, project: str, service: str) -> str:
    value = run(
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        compose_file,
        "ps",
        "-q",
        service,
    )
    if not value:
        raise RuntimeError(f"{service} container is unavailable")
    return value


def inspect(container_id: str) -> dict[str, Any]:
    value = json.loads(run("docker", "inspect", container_id))
    if not isinstance(value, list) or len(value) != 1:
        raise RuntimeError("docker inspect envelope rejected")
    return value[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-file", required=True)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    ids = {
        service: service_id(args.compose_file, args.project, service)
        for service in ("backend", "track-catalog", "external-connector")
    }
    deadline = time.monotonic() + 60
    while True:
        details = {
            service: inspect(container_id) for service, container_id in ids.items()
        }
        health_states = {
            service: item["State"].get("Health", {}).get("Status")
            for service, item in details.items()
        }
        if set(health_states.values()) == {"healthy"}:
            break
        if any(not item["State"].get("Running", False) for item in details.values()):
            raise RuntimeError("a Demo service stopped during health verification")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Demo service health timeout: {health_states}")
        time.sleep(1)
    networks = {
        service: sorted(item["NetworkSettings"]["Networks"])
        for service, item in details.items()
    }
    private_name = f"{args.project}_demo-private"
    egress_name = f"{args.project}_demo-egress"

    if set(networks["backend"]) != {"companion-private", private_name}:
        raise RuntimeError("backend network boundary rejected")
    if set(networks["track-catalog"]) != {private_name}:
        raise RuntimeError("track-catalog network boundary rejected")
    if set(networks["external-connector"]) != {private_name, egress_name}:
        raise RuntimeError("external-connector network boundary rejected")

    egress = json.loads(run("docker", "network", "inspect", egress_name))[0]
    egress_members = set((egress.get("Containers") or {}).keys())
    if egress_members != {ids["external-connector"]}:
        raise RuntimeError("external-connector is not the sole Demo egress member")

    if details["backend"]["HostConfig"].get("PortBindings"):
        raise RuntimeError("backend unexpectedly publishes a host port")
    runtime_ports = details["backend"]["NetworkSettings"].get("Ports") or {}
    if any(runtime_ports.values()):
        raise RuntimeError("backend unexpectedly exposes a runtime port binding")
    backend_ip = details["backend"]["NetworkSettings"]["Networks"][private_name].get(
        "IPAddress", ""
    )
    if not backend_ip:
        raise RuntimeError("backend Demo-private address is unavailable")

    with urllib.request.urlopen(
        f"http://{backend_ip}:8000/v1/live/health", timeout=5
    ) as response:
        health = json.load(response)
    deployment = health.get("deployment")
    if deployment != {"backend": "DGX_SPARK", "console_access": "SSH_LOOPBACK"}:
        raise RuntimeError("backend deployment identity rejected")

    components = {
        item.get("component"): item for item in health.get("components", [])
    }
    result = {
        "status": "PASS",
        "services": {
            service: {
                "running": details[service]["State"]["Running"],
                "healthy": details[service]["State"].get("Health", {}).get("Status"),
                "networks": networks[service],
            }
            for service in ids
        },
        "backend_published_port": None,
        "console_tunnel_target": "demo-private/backend:8000",
        "deployment": deployment,
        "model_health": {
            name: {
                "available": components.get(name, {}).get("available", False),
                "status": components.get(name, {}).get("status", "MISSING"),
            }
            for name in ("STEP3", "STEPAUDIO")
        },
        "weather_health": components.get("WEATHER_EGRESS", {}),
        "audius_health": components.get("AUDIUS_MUSIC", {}),
        "provider_probe_performed_by_health": False,
        "existing_model_containers_restarted": False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
