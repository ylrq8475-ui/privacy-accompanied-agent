"""Short-lived worker used to verify Phase 5 process-restart recovery."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from external_connector.mock import MockExternalConnector

from backend.app.api import DemoASGIApp
from backend.app.mocks import MockAC, MockMusic
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.actions import (
    ActionProposal,
    ActionResult,
    ActionType,
    AuthorizationStatus,
    ExecutionStatus,
)


SEEDED_AT = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
RECOVERED_AT = SEEDED_AT + timedelta(minutes=10)


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _proposal(name: str, expires_at: datetime) -> ActionProposal:
    return ActionProposal.model_validate(
        {
            "action_id": f"music-restart-{name}",
            "session_id": f"session-restart-{name}",
            "action_type": "PLAY_MUSIC",
            "payload": {"track_id": "calm_piano_01"},
            "proposed_at": SEEDED_AT,
            "expires_at": expires_at,
        }
    )


async def _run_lifespan(app: DemoASGIApp, operation) -> object:  # type: ignore[no-untyped-def]
    incoming: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    async def receive() -> dict[str, str]:
        return await incoming.get()

    async def send(message: dict[str, str]) -> None:
        await outgoing.put(message)

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await incoming.put({"type": "lifespan.startup"})
    startup = await outgoing.get()
    if startup.get("type") != "lifespan.startup.complete":
        raise RuntimeError("ASGI startup did not complete")
    try:
        return operation()
    finally:
        await incoming.put({"type": "lifespan.shutdown"})
        shutdown = await outgoing.get()
        if shutdown.get("type") != "lifespan.shutdown.complete":
            raise RuntimeError("ASGI shutdown did not complete")
        await task


def _seed(store: SQLitePersistence) -> dict[str, object]:
    proposals = {
        "pending": _proposal("pending", SEEDED_AT + timedelta(minutes=30)),
        "approved": _proposal("approved", SEEDED_AT + timedelta(minutes=30)),
        "expired": _proposal("expired", SEEDED_AT + timedelta(minutes=5)),
        "running": _proposal("running", SEEDED_AT + timedelta(minutes=30)),
        "terminal": _proposal("terminal", SEEDED_AT + timedelta(minutes=30)),
    }
    store.create_actions(proposals.values())
    for name in ("approved", "expired", "running", "terminal"):
        store.update_authorization(
            proposals[name].action_id, AuthorizationStatus.APPROVED, SEEDED_AT
        )
    store.mark_running(proposals["running"].action_id)
    store.mark_running(proposals["terminal"].action_id)
    store.finish_action(
        ActionResult(
            action_id=proposals["terminal"].action_id,
            action_type=ActionType.PLAY_MUSIC,
            execution_status=ExecutionStatus.SUCCEEDED,
            result={
                "mock": True,
                "physical_action_performed": False,
                "code": "SEEDED_TERMINAL",
            },
            completed_at=SEEDED_AT + timedelta(seconds=1),
        )
    )
    return {"seeded": len(proposals)}


def _recover(
    app: DemoASGIApp,
    store: SQLitePersistence,
    music: MockMusic,
    ac: MockAC,
) -> dict[str, object]:
    actions = {
        action.action_id.removeprefix("music-restart-"): action
        for action in store.list_actions()
    }
    states = {
        name: f"{action.authorization_status.value}/{action.execution_status.value}"
        for name, action in actions.items()
    }
    expected = {
        "pending": "PENDING/NOT_STARTED",
        "approved": "APPROVED/NOT_STARTED",
        "expired": "EXPIRED/NOT_STARTED",
        "running": "APPROVED/FAILED",
        "terminal": "APPROVED/SUCCEEDED",
    }
    interrupted = actions["running"].result or {}
    console_reloaded = app.console_assets.resolve("/console/").status == 200
    connector = MockExternalConnector()
    connector.send(
        {
            "request_id": "request-restart-recreated",
            "source_agent": "phase5-recovery",
            "destination": "WEATHER_API",
            "network_scope": "INTERNET",
            "payload": {"city_code": "310000"},
            "created_at": RECOVERED_AT,
        }
    )
    auto_execution_count = len(music.executed_action_ids) + len(ac.executed_action_ids)
    passed = (
        states == expected
        and interrupted.get("code") == "INTERRUPTED_BY_RESTART"
        and interrupted.get("physical_action_performed") is False
        and auto_execution_count == 0
        and console_reloaded
        and len(connector.sent_requests) == 1
    )
    return {
        "classification": "DEMO_OWNED_BACKEND_PROCESS_RESTART",
        "passed": passed,
        "auto_execution_count": auto_execution_count,
        "states": states,
        "console_reloaded": console_reloaded,
        "connector_recreated": len(connector.sent_requests) == 1,
    }


async def _execute(args: argparse.Namespace) -> dict[str, object]:
    store = SQLitePersistence(args.database)
    music = MockMusic()
    ac = MockAC()
    orchestrator = Orchestrator(
        clock=FixedClock(SEEDED_AT if args.mode == "seed" else RECOVERED_AT),
        persistence=store,
        music=music,
        ac=ac,
    )
    app = DemoASGIApp(orchestrator)
    operation = (
        (lambda: _seed(store))
        if args.mode == "seed"
        else (lambda: _recover(app, store, music, ac))
    )
    return await _run_lifespan(app, operation)  # type: ignore[return-value]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("seed", "recover"))
    return parser.parse_args()


def main() -> int:
    result = asyncio.run(_execute(parse_args()))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
