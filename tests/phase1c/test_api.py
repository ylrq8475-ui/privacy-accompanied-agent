from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.app.api import DemoASGIApp
from backend.app.orchestrator import Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.schemas.step3 import StateLabel
from tests.phase1c.helpers import FixedClock


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


class PersistenceAPIContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "demo.sqlite3"
        self.store = SQLitePersistence(self.path)
        self.orchestrator = Orchestrator(
            clock=FixedClock(), persistence=self.store
        )
        self.app = DemoASGIApp(self.orchestrator)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_memory_confirm_list_delete_and_not_found(self) -> None:
        request = {
            "context": "workday_evening",
            "preference": "calm_piano",
            "confirmed": True,
        }
        status, memory = await http_request(
            self.app, "POST", "/v1/memories/confirm", request
        )
        self.assertEqual(status, 201)
        status, body = await http_request(self.app, "GET", "/v1/memories")
        self.assertEqual(status, 200)
        self.assertEqual(body["memories"], [memory])

        status, body = await http_request(
            self.app, "DELETE", f"/v1/memories/{memory['memory_id']}"
        )
        self.assertEqual((status, body), (200, {"deleted": True}))
        status, body = await http_request(
            self.app, "DELETE", f"/v1/memories/{memory['memory_id']}"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "MEMORY_NOT_FOUND")

    async def test_invalid_memory_requests_never_write(self) -> None:
        invalid_requests = (
            {"context": "workday_evening", "preference": "calm_piano", "confirmed": False},
            {"context": "workday_evening", "confirmed": True},
            {
                "context": "workday_evening",
                "preference": "calm_piano",
                "confirmed": True,
                "extra": "forbidden",
            },
            {"context": "workday_evening", "preference": "calm_piano", "confirmed": 1},
            {"context": "workday_evening", "preference": 3, "confirmed": True},
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                status, body = await http_request(
                    self.app, "POST", "/v1/memories/confirm", request
                )
                self.assertEqual(status, 422)
                self.assertEqual(body["error"], "VALIDATION_ERROR")
        self.assertEqual(self.store.list_memories(), [])

    async def test_action_query_reads_persisted_action_across_orchestrators(self) -> None:
        session = self.orchestrator.start_session()
        self.orchestrator.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
        music_id = session.music_action.action_id
        self.orchestrator.authorize(session.session_id, music_id, True)
        restarted_app = DemoASGIApp(
            Orchestrator(
                clock=FixedClock(), persistence=SQLitePersistence(self.path)
            )
        )
        status, action = await http_request(
            restarted_app, "GET", f"/v1/actions/{music_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(action["action_id"], music_id)
        self.assertEqual(action["authorization_status"], "APPROVED")
        self.assertEqual(action["execution_status"], "SUCCEEDED")
        self.assertTrue(action["result"]["mock"])
        self.assertFalse(action["result"]["physical_action_performed"])
        self.assertIsNotNone(action["expires_at"])

        status, body = await http_request(
            restarted_app, "GET", "/v1/actions/music-missing"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "ACTION_NOT_FOUND")

    async def test_lifespan_initializes_lazy_database_without_execution(self) -> None:
        self.assertFalse(self.path.exists())
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        await incoming.put({"type": "lifespan.startup"})
        await incoming.put({"type": "lifespan.shutdown"})
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        await self.app({"type": "lifespan"}, receive, send)
        self.assertTrue(self.path.exists())
        self.assertEqual(
            [item["type"] for item in outgoing],
            ["lifespan.startup.complete", "lifespan.shutdown.complete"],
        )
        self.assertEqual(self.orchestrator.music.executed_action_ids, [])
        self.assertEqual(self.orchestrator.ac.executed_action_ids, [])


if __name__ == "__main__":
    unittest.main()
