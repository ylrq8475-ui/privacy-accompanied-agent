from __future__ import annotations

from collections import deque

from backend.app.adapters import AdapterError, TransportResponse


class FakeTransport:
    def __init__(self, *responses: TransportResponse | AdapterError) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        base_url: str,
        path: str,
        body: dict[str, object] | None,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        self.requests.append(
            {
                "method": method,
                "base_url": base_url,
                "path": path,
                "body": body,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        response = self.responses.popleft()
        if isinstance(response, AdapterError):
            raise response
        return response


class FailingAudio:
    def health(self):
        raise AssertionError("health is not used in E2E")

    def transcribe(self):
        raise AdapterError("ASR_FILENAME_UNAVAILABLE", "synthetic shared filename absent")

    def synthesize(self, text: str):
        raise AdapterError("MODEL_UNAVAILABLE", "synthetic TTS unavailable")


class FailingStep3:
    def __init__(self) -> None:
        self.calls = 0

    def health(self):
        raise AssertionError("health is not used in E2E")

    def analyze(self, transcript: str, jpeg: bytes | None = None):
        self.calls += 1
        raise AdapterError("MODEL_UNAVAILABLE", "synthetic Step3 unavailable")
