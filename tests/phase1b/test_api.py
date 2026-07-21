from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from backend.app.api import DemoASGIApp
from backend.app.orchestrator import Orchestrator
from backend.app.schemas.step3 import StateLabel


async def http_request(
    app: DemoASGIApp,
    method: str,
    path: str,
    body: dict[str, Any] | bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    if isinstance(body, dict):
        encoded = json.dumps(body).encode("utf-8")
    elif isinstance(body, bytes):
        encoded = body
    else:
        encoded = b""
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": encoded, "more_body": False})
    outgoing: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return await incoming.get()

    async def send(message: dict[str, Any]) -> None:
        outgoing.append(message)

    await app(
        {"type": "http", "method": method, "path": path, "headers": []},
        receive,
        send,
    )
    status = next(item["status"] for item in outgoing if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"") for item in outgoing if item["type"] == "http.response.body"
    )
    return status, json.loads(response_body)


class APIContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.orchestrator = Orchestrator()
        self.app = DemoASGIApp(self.orchestrator)

    async def test_health_and_unknown_route(self) -> None:
        status, body = await http_request(self.app, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "PURE_MOCK")
        status, body = await http_request(self.app, "GET", "/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "NOT_FOUND")

    async def test_http_api_runs_full_mock_scenario(self) -> None:
        status, session = await http_request(
            self.app, "POST", "/v1/mock/sessions", {"scenario": "success"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(session["state"], "CLARIFICATION_REQUIRED")
        session_id = session["session_id"]

        status, session = await http_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session_id}/clarifications",
            {"answer": "PHYSICAL_FATIGUE"},
        )
        self.assertEqual(status, 200)
        music_id = session["music_action"]["action_id"]
        ac_id = session["ac_action"]["action_id"]

        status, session = await http_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session_id}/actions/{music_id}/authorization",
            {"approved": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(session["state"], "WAITING_AC_AUTHORIZATION")

        status, session = await http_request(
            self.app,
            "POST",
            f"/v1/mock/sessions/{session_id}/actions/{ac_id}/authorization",
            {"approved": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(session["state"], "COMPLETED")

        status, events = await http_request(
            self.app, "GET", f"/v1/mock/sessions/{session_id}/events"
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(item["event_type"] == "STATE_TRANSITION" for item in events["events"]))

    async def test_request_models_reject_extra_fields_and_wrong_types(self) -> None:
        status, body = await http_request(
            self.app,
            "POST",
            "/v1/mock/sessions",
            {"scenario": "success", "real_model": True},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "VALIDATION_ERROR")

        status, body = await http_request(
            self.app,
            "POST",
            "/v1/mock/sessions",
            {"scenario": 1},
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "VALIDATION_ERROR")

    async def test_invalid_json_and_unknown_session_are_rejected(self) -> None:
        status, body = await http_request(
            self.app, "POST", "/v1/mock/sessions", b"{not-json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "INVALID_JSON")
        status, body = await http_request(
            self.app, "GET", "/v1/mock/sessions/session-missing"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "SESSION_NOT_FOUND")

    async def test_wrong_action_authorization_returns_conflict(self) -> None:
        session = self.orchestrator.start_session()
        self.orchestrator.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
        status, body = await http_request(
            self.app,
            "POST",
            (
                f"/v1/mock/sessions/{session.session_id}/actions/"
                f"{session.ac_action.action_id}/authorization"
            ),
            {"approved": True},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "INVALID_OPERATION")

    async def test_websocket_replays_and_pushes_real_audit_events(self) -> None:
        session = self.orchestrator.start_session()
        existing_count = len(self.orchestrator.audit_log.list_events(session.session_id))
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        await incoming.put({"type": "websocket.connect"})
        outgoing: list[dict[str, Any]] = []
        replay_ready = asyncio.Event()
        live_ready = asyncio.Event()

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)
            sent_events = sum(item["type"] == "websocket.send" for item in outgoing)
            if sent_events >= existing_count:
                replay_ready.set()
            if sent_events >= existing_count + 1:
                live_ready.set()

        task = asyncio.create_task(
            self.app(
                {
                    "type": "websocket",
                    "path": f"/v1/mock/sessions/{session.session_id}/events",
                },
                receive,
                send,
            )
        )
        await asyncio.wait_for(replay_ready.wait(), timeout=1)
        self.orchestrator.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
        await asyncio.wait_for(live_ready.wait(), timeout=1)
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(outgoing[0]["type"], "websocket.accept")
        pushed = [
            json.loads(item["text"])
            for item in outgoing
            if item["type"] == "websocket.send"
        ]
        self.assertGreater(len(pushed), existing_count)
        self.assertTrue(
            any(
                item["event_type"] == "STATE_TRANSITION"
                and item["payload"]["to_state"] == "USER_CLARIFIED"
                for item in pushed[existing_count:]
            )
        )


if __name__ == "__main__":
    unittest.main()
