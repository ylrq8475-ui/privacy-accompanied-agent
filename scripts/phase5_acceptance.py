"""Run Phase 5 samples, E2E, faults, recovery, and emit auditable reports."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from external_connector.contracts import ConnectorRejectedError
from external_connector.weather import ExternalConnectorTransportError, RealExternalConnector

from backend.app.adapters import (
    AdapterError,
    PerceptionSource,
    Phase3Settings,
    Step3Adapter,
    StepAudioAdapter,
    TransportResponse,
    VisionAdapter,
)
from backend.app.api import DemoASGIApp
from backend.app.live import FALLBACK_TRANSCRIPT, LiveCoordinator
from backend.app.local_music import LocalMusicPlayer
from backend.app.mocks import MockAC, MockASR, MockStep3
from backend.app.orchestrator import MockScenario, Orchestrator
from backend.app.persistence import SQLitePersistence
from backend.app.privacy_guard import PrivacyDecision, check_payload
from backend.app.schemas.actions import (
    ACActionPayload,
    ACMode,
    ActionAuthorization,
    ActionProposal,
    ActionType,
    AuthorizationStatus,
)
from backend.app.schemas.events import NetworkScope
from backend.app.schemas.network import NetworkDestination
from backend.app.schemas.persistence import MemoryConfirmationRequest
from backend.app.schemas.step3 import StateLabel
from backend.app.state_machine import DemoState
from scripts.phase5_metrics import (
    AcceptanceReport,
    BenchmarkSample,
    BenchmarkSummary,
    E2EResult,
    FaultResult,
    RecoveryResult,
    assert_report_safe,
    render_markdown,
    summarize,
    write_report,
)


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _synthetic_music_root(root: Path) -> Path:
    """Create a non-played fixture for deterministic acceptance paths."""
    music_root = root / "music"
    track_path = music_root / "tracks" / "calm_piano_01.flac"
    track_path.parent.mkdir(parents=True, exist_ok=True)
    track_path.write_bytes(b"synthetic-test-audio-not-for-playback")
    (music_root / "catalog.json").write_text(
        json.dumps(
            {
                "tracks": [
                    {
                        "track_id": "calm_piano_01",
                        "path": "tracks/calm_piano_01.flac",
                        "sha256": hashlib.sha256(track_path.read_bytes()).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return music_root


class FixedClock:
    def __init__(self, value: datetime = SYNTHETIC_NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeWeatherTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def fetch(self, url: str, timeout_seconds: float) -> bytes:
        if self.fail:
            raise ExternalConnectorTransportError("synthetic connector outage")
        return b'{"current":{"temperature_2m":24.0,"weather_code":1}}'


class RecordingPlaybackBackend:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.play_count = 0

    def play(self, path: Path) -> None:
        if self.fail:
            raise RuntimeError("synthetic audio failure")
        self.play_count += 1

    def close(self) -> None:
        pass


class FailingAudio:
    def transcribe(self):
        raise AdapterError("MODEL_UNAVAILABLE", "synthetic unavailable")

    def synthesize(self, text: str):
        raise AdapterError("MODEL_UNAVAILABLE", "synthetic unavailable")

    def health(self):
        raise AssertionError("health is outside E2E")


class FailingStep3:
    def analyze(self, transcript: str, jpeg: bytes | None = None):
        raise AdapterError("MODEL_UNAVAILABLE", "synthetic unavailable")

    def health(self):
        raise AssertionError("health is outside E2E")


class InvalidStep3Transport:
    def request(self, *args, **kwargs) -> TransportResponse:  # type: ignore[no-untyped-def]
        return TransportResponse(
            status=200,
            body={"choices": [{"message": {"content": "{}"}}]},
            latency_ms=1,
        )


class DelayedFailingAudio(FailingAudio):
    def synthesize(self, text: str):
        time.sleep(0.05)
        raise AdapterError("TTS_TIMEOUT", "synthetic delayed timeout")


def _error_code(error: Exception) -> str:
    if isinstance(error, AdapterError):
        return error.code
    if isinstance(error, ConnectorRejectedError):
        return "CONNECTOR_REJECTED"
    if isinstance(error, ExternalConnectorTransportError):
        return "CONNECTOR_TRANSPORT_ERROR"
    return type(error).__name__.upper()


def _sample_sync(
    count: int,
    operation: Callable[[int], str],
) -> list[BenchmarkSample]:
    samples: list[BenchmarkSample] = []
    for index in range(1, count + 1):
        started = time.perf_counter_ns()
        try:
            outcome = operation(index)
            success = True
            error_code = None
        except Exception as error:
            outcome = "FAILED"
            success = False
            error_code = _error_code(error)
        latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        samples.append(
            BenchmarkSample(
                index=index,
                latency_ms=latency_ms,
                success=success,
                outcome=outcome,
                error_code=error_code,
            )
        )
    return samples


async def _json_request(
    app: DemoASGIApp,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    encoded = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    await incoming.put({"type": "http.request", "body": encoded, "more_body": False})
    outgoing: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return await incoming.get()

    async def send(message: dict[str, object]) -> None:
        outgoing.append(message)

    await app(
        {"type": "http", "method": method, "path": path, "headers": []},
        receive,
        send,
    )
    status = next(
        int(message["status"])
        for message in outgoing
        if message["type"] == "http.response.start"
    )
    raw = b"".join(
        message.get("body", b"")  # type: ignore[arg-type]
        for message in outgoing
        if message["type"] == "http.response.body"
    )
    return status, json.loads(raw)


def _proposal(index: int, action_type: ActionType = ActionType.PLAY_MUSIC) -> ActionProposal:
    if action_type is ActionType.PLAY_MUSIC:
        action_id = f"music-phase5-{index}"
        payload = {"track_id": "calm_piano_01"}
    else:
        action_id = f"ac-phase5-{index}"
        payload = {
            "device_id": "living_room_ac_mock",
            "mode": "heat",
            "target_temperature": 24,
            "duration_minutes": 30,
        }
    return ActionProposal.model_validate(
        {
            "action_id": action_id,
            "session_id": f"session-phase5-{index}",
            "action_type": action_type.value,
            "payload": payload,
            "proposed_at": SYNTHETIC_NOW,
            "expires_at": SYNTHETIC_NOW + timedelta(minutes=5),
        }
    )


def _approved(proposal: ActionProposal) -> ActionAuthorization:
    return ActionAuthorization(
        action_id=proposal.action_id,
        action_type=proposal.action_type,
        authorization_status=AuthorizationStatus.APPROVED,
        decided_at=SYNTHETIC_NOW,
        expires_at=proposal.expires_at,
    )


async def _benchmark_authorization(count: int) -> BenchmarkSummary:
    samples: list[BenchmarkSample] = []
    for index in range(1, count + 1):
        orchestrator = Orchestrator(clock=FixedClock())
        app = DemoASGIApp(orchestrator)
        _, session = await _json_request(app, "POST", "/v1/mock/sessions", {"scenario": "success"})
        session_id = str(session["session_id"])
        _, session = await _json_request(
            app,
            "POST",
            f"/v1/mock/sessions/{session_id}/clarifications",
            {"answer": "PHYSICAL_FATIGUE"},
        )
        action_id = str(session["music_action"]["action_id"])  # type: ignore[index]
        started = time.perf_counter_ns()
        try:
            status, result = await _json_request(
                app,
                "POST",
                f"/v1/mock/sessions/{session_id}/actions/{action_id}/authorization",
                {"approved": False},
            )
            if status != 200 or result.get("state") != "WAITING_AC_AUTHORIZATION":
                raise RuntimeError("authorization response rejected")
            success, outcome, error_code = True, "REJECTED_AS_REQUESTED", None
        except Exception as error:
            success, outcome, error_code = False, "FAILED", _error_code(error)
        samples.append(
            BenchmarkSample(
                index=index,
                latency_ms=(time.perf_counter_ns() - started) / 1_000_000,
                success=success,
                outcome=outcome,
                error_code=error_code,
            )
        )
    return summarize(
        "ACTION_AUTHORIZATION",
        "ASGI_DETERMINISTIC_POLICY",
        samples,
        minimum_success_count=count,
    )


async def run_benchmarks(count: int) -> list[BenchmarkSummary]:
    settings = Phase3Settings.from_environment()
    summaries: list[BenchmarkSummary] = []

    mock_step3 = MockStep3()
    summaries.append(
        summarize(
            "MOCK_STEP3",
            "MOCK",
            _sample_sync(count, lambda _: _mock_step3_call(mock_step3)),
            minimum_success_count=count,
        )
    )
    real_step3 = Step3Adapter(settings.step3_url)
    summaries.append(
        summarize(
            "REAL_STEP3",
            "LOCAL_ADAPTER",
            _sample_sync(count, lambda _: _real_step3_call(real_step3)),
            minimum_success_count=0,
        )
    )
    mock_asr = MockASR()
    summaries.append(
        summarize(
            "MOCK_ASR",
            "MOCK",
            _sample_sync(count, lambda _: _mock_asr_call(mock_asr)),
            minimum_success_count=count,
        )
    )
    real_asr = StepAudioAdapter(settings.stepaudio_url, "synthetic-demo.wav")
    summaries.append(
        summarize(
            "REAL_ASR",
            "LOCAL_ADAPTER_SYNTHETIC_FILENAME",
            _sample_sync(count, lambda _: _real_asr_call(real_asr)),
            minimum_success_count=0,
        )
    )

    weather = RealExternalConnector()
    summaries.append(
        summarize(
            "WEATHER_CONNECTOR",
            "REAL_OPEN_METEO_INTERNET",
            _sample_sync(count, lambda index: _weather_call(weather, index)),
            minimum_success_count=0,
        )
    )

    with tempfile.TemporaryDirectory() as temporary:
        memory_store = SQLitePersistence(Path(temporary) / "memory.sqlite3")
        summaries.append(
            summarize(
                "SQLITE_MEMORY",
                "TEMP_SQLITE_ROUND_TRIP",
                _sample_sync(count, lambda _: _memory_call(memory_store)),
                minimum_success_count=count,
            )
        )

    summaries.append(await _benchmark_authorization(count))
    summaries.append(
        summarize(
            "PRIVACY_GUARD",
            "DETERMINISTIC_ALLOWLIST",
            _sample_sync(count, _privacy_call),
            minimum_success_count=count,
        )
    )

    player = LocalMusicPlayer()
    try:
        music_samples = _sample_sync(count, lambda index: _music_call(player, index))
    finally:
        player.close()
    summaries.append(
        summarize(
            "MUSIC_EXECUTOR",
            "REAL_LOCAL_MINIAUDIO",
            music_samples,
            minimum_success_count=count,
        )
    )

    ac = MockAC()
    summaries.append(
        summarize(
            "AC_MOCK",
            "LOCAL_MOCK",
            _sample_sync(count, lambda index: _ac_call(ac, index)),
            minimum_success_count=count,
        )
    )
    return summaries


def _mock_step3_call(adapter: MockStep3) -> str:
    adapter.analyze()
    return "STRICT_SCHEMA"


def _real_step3_call(adapter: Step3Adapter) -> str:
    adapter.analyze("Synthetic user feels tired after work.")
    return "STRICT_SCHEMA"


def _mock_asr_call(adapter: MockASR) -> str:
    adapter.transcribe()
    return "TRANSCRIPT"


def _real_asr_call(adapter: StepAudioAdapter) -> str:
    adapter.transcribe()
    return "TRANSCRIPT"


def _weather_call(connector: RealExternalConnector, index: int) -> str:
    response = connector.send(
        {
            "request_id": f"request-phase5-{index}",
            "source_agent": "weather-agent",
            "destination": "WEATHER_API",
            "network_scope": "INTERNET",
            "payload": {"city_code": "310000"},
            "created_at": datetime.now(timezone.utc),
        }
    )
    if response.status.value != "SUCCEEDED":
        raise RuntimeError("weather response failed")
    return "REAL_API"


def _memory_call(store: SQLitePersistence) -> str:
    record = store.confirm_memory(
        MemoryConfirmationRequest(
            context="workday_evening", preference="calm_piano", confirmed=True
        ),
        datetime.now(timezone.utc),
    )
    if not store.list_memories() or not store.delete_memory(record.memory_id):
        raise RuntimeError("memory round trip failed")
    return "CONFIRM_LIST_DELETE"


def _privacy_call(index: int) -> str:
    if index <= 10:
        result = check_payload(
            NetworkScope.INTERNET,
            NetworkDestination.WEATHER_API,
            {"city_code": "310000"},
        )
        if result.decision is not PrivacyDecision.ALLOWED:
            raise RuntimeError("allowed privacy payload rejected")
        return "EXPECTED_ALLOWED"
    result = check_payload(
        NetworkScope.INTERNET,
        NetworkDestination.WEATHER_API,
        {"city_code": "310000", "raw_audio": "synthetic-forbidden"},
    )
    if result.decision is not PrivacyDecision.REJECTED:
        raise RuntimeError("forbidden privacy payload allowed")
    return "EXPECTED_REJECTED"


def _music_call(player: LocalMusicPlayer, index: int) -> str:
    proposal = _proposal(index)
    result = player.execute(proposal, _approved(proposal), SYNTHETIC_NOW)
    if result.result.get("mock") is not False or result.result.get("playback_started") is not True:
        raise RuntimeError("music playback did not start")
    return "PLAYBACK_STARTED"


def _ac_call(ac: MockAC, index: int) -> str:
    proposal = _proposal(index, ActionType.SET_AC)
    result = ac.execute(proposal, SYNTHETIC_NOW)
    if (
        result.result.get("mock") is not True
        or result.result.get("physical_action_performed") is not False
        or result.result.get("message") != "模拟执行成功"
    ):
        raise RuntimeError("AC Mock result is inaccurate")
    return "SIMULATED_SUCCESS"


async def run_fixed_e2e(count: int = 5) -> E2EResult:
    session_ids: list[str] = []
    success_count = 0
    with tempfile.TemporaryDirectory() as temporary:
        orchestrator = Orchestrator(
            clock=FixedClock(),
            persistence=SQLitePersistence(Path(temporary) / "fixed-e2e.sqlite3"),
        )
        for _ in range(count):
            session = orchestrator.start_session(MockScenario.SUCCESS)
            session = orchestrator.clarify(
                session.session_id, StateLabel.PHYSICAL_FATIGUE
            )
            music_id = session.music_action.action_id
            ac_id = session.ac_action.action_id
            session = orchestrator.authorize(session.session_id, music_id, True)
            session = orchestrator.authorize(session.session_id, ac_id, True)
            events = orchestrator.audit_log.list_events(session.session_id)
            transitions = [e for e in events if e.event_type == "STATE_TRANSITION"]
            event_types = {event.event_type for event in events}
            ok = (
                session.state is DemoState.COMPLETED
                and music_id != ac_id
                and len(transitions) == len(session.state_history) - 1
                and DemoState.MEMORY_RETRIEVED in session.state_history
                and "INTERNET_MOCK_REQUEST" in event_types
                and "POLICY_CLARIFICATION_DECISION" in event_types
                and sum(e.event_type == "ACTION_AUTHORIZATION_UPDATED" for e in events)
                == 2
                and session.results[ac_id].result.get("mock") is True
            )
            success_count += int(ok)
            session_ids.append(session.session_id)
    unique = len(set(session_ids))
    return E2EResult(
        mode="FIXED_MOCK_DEMO",
        count=count,
        success_count=success_count,
        unique_session_count=unique,
        passed=success_count == count and unique == count,
        details={
            "independent_authorization": True,
            "ac_physical_action_performed": False,
            "input": "SYNTHETIC",
        },
    )


async def run_degraded_e2e(count: int = 5) -> E2EResult:
    session_ids: list[str] = []
    success_count = 0
    model_fallback_count = 0
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "degraded-e2e.sqlite3"
        music_root = _synthetic_music_root(Path(temporary))
        for _ in range(count):
            playback = RecordingPlaybackBackend()
            orchestrator = Orchestrator(
                clock=FixedClock(),
                persistence=SQLitePersistence(database),
                live_connector=RealExternalConnector(
                    transport=FakeWeatherTransport(), clock=FixedClock()
                ),
                live_music=LocalMusicPlayer(music_root, backend=playback),
            )
            live = LiveCoordinator(
                orchestrator,
                vision=VisionAdapter(
                    observers=[
                        (PerceptionSource.STATIC_SYNTHETIC, lambda: (True, None))
                    ]
                ),
                audio=FailingAudio(),
                step3=FailingStep3(),
                confirmation_samples=2,
            )
            app = DemoASGIApp(orchestrator, live=live)
            try:
                await _json_request(app, "POST", "/v1/live/perception/poll")
                _, response = await _json_request(
                    app, "POST", "/v1/live/perception/poll"
                )
                session = response["session"]
                session_id = str(session["session_id"])
                _, session = await _json_request(
                    app,
                    "POST",
                    f"/v1/live/sessions/{session_id}/transcript",
                    {"text": FALLBACK_TRANSCRIPT},
                )
                _, session = await _json_request(
                    app,
                    "POST",
                    f"/v1/live/sessions/{session_id}/clarifications",
                    {"answer": "PHYSICAL_FATIGUE"},
                )
                music_id = str(session["music_action"]["action_id"])  # type: ignore[index]
                ac_id = str(session["ac_action"]["action_id"])  # type: ignore[index]
                for action_id in (music_id, ac_id):
                    _, session = await _json_request(
                        app,
                        "POST",
                        f"/v1/live/sessions/{session_id}/actions/{action_id}/authorization",
                        {"approved": True},
                    )
                model_fallback = session.get("model_source") == "RULE_FALLBACK"
                events = orchestrator.audit_log.list_events(session_id)
                event_types = {event.event_type for event in events}
                privacy_allowed = any(
                    event.event_type == "INTERNET_REQUEST"
                    and event.payload.get("privacy_result") == "ALLOWED"
                    for event in events
                )
                ok = (
                    session.get("state") == "COMPLETED"
                    and music_id != ac_id
                    and playback.play_count == 1
                    and model_fallback
                    and {
                        "VISION_OBSERVATION",
                        "TEXT_FALLBACK_INPUT",
                        "STEP3_MODEL_CALL",
                        "POLICY_CLARIFICATION_DECISION",
                        "LOCAL_ACTION_REQUEST",
                    }.issubset(event_types)
                    and any(
                        event.event_type == "STATE_TRANSITION"
                        and event.payload.get("to_state") == "MEMORY_RETRIEVED"
                        for event in events
                    )
                    and privacy_allowed
                    and sum(
                        event.event_type == "ACTION_AUTHORIZATION_UPDATED"
                        for event in events
                    )
                    == 2
                    and session["results"][ac_id]["result"]["mock"] is True  # type: ignore[index]
                )
                success_count += int(ok)
                model_fallback_count += int(model_fallback)
                session_ids.append(session_id)
            finally:
                live.close()
    unique = len(set(session_ids))
    return E2EResult(
        mode="MODEL_UNAVAILABLE_DEGRADED_DEMO",
        count=count,
        success_count=success_count,
        unique_session_count=unique,
        passed=(
            success_count == count
            and unique == count
            and model_fallback_count == count
        ),
        details={
            "model_fallback_count": model_fallback_count,
            "audio_backend": "RECORDING_TEST_DOUBLE",
            "weather_transport": "SYNTHETIC_TEST_DOUBLE",
        },
    )


def run_fault_matrix() -> list[FaultResult]:
    results: list[FaultResult] = []

    def record(scenario: str, passed: bool, outcome: str) -> None:
        results.append(FaultResult(scenario=scenario, passed=passed, outcome=outcome))

    record(
        "CAMERA_FAILURE",
        Orchestrator().start_session(MockScenario.CAMERA_FAILED).state
        is DemoState.CAMERA_FAILED,
        "SAFE_TERMINAL",
    )
    record(
        "ASR_FAILURE",
        Orchestrator().start_session(MockScenario.ASR_FAILED).state
        is DemoState.ASR_FAILED,
        "SAFE_TERMINAL",
    )
    record(
        "STEP3_TIMEOUT",
        Orchestrator().start_session(MockScenario.MODEL_TIMEOUT).state
        is DemoState.MODEL_TIMEOUT,
        "SAFE_TERMINAL",
    )
    try:
        Step3Adapter("http://step3-vl:8000", transport=InvalidStep3Transport()).analyze(
            "Synthetic transcript"
        )
        invalid_passed = False
    except AdapterError as error:
        invalid_passed = error.code == "STEP3_SCHEMA_REJECTED"
    record("STEP3_INVALID_SCHEMA", invalid_passed, "REJECTED")

    record(
        "WEATHER_API_FAILURE",
        Orchestrator().start_session(MockScenario.API_FAILED).state
        is DemoState.API_FAILED,
        "SAFE_TERMINAL",
    )
    with tempfile.TemporaryDirectory() as temporary:
        unavailable = Orchestrator(
            clock=FixedClock(),
            persistence=SQLitePersistence(Path(temporary) / "weather.sqlite3"),
            live_connector=RealExternalConnector(
                transport=FakeWeatherTransport(fail=True), clock=FixedClock()
            ),
        )
        session = unavailable.begin_live_session(
            perception_source="STATIC_SYNTHETIC", degraded_reasons=[]
        )
        events = unavailable.audit_log.list_events(session.session_id)
        fixed = next(
            event.payload["details"]["weather"]["source"]
            for event in events
            if event.event_type == "STATE_TRANSITION"
            and event.payload["to_state"] == "CONTEXT_READY"
        )
        record("EXTERNAL_CONNECTOR_UNAVAILABLE", fixed == "FIXED_DEMO", "FIXED_DEMO")

    tts_orchestrator = Orchestrator()
    tts_session = tts_orchestrator.start_session()
    tts_live = LiveCoordinator(tts_orchestrator, audio=DelayedFailingAudio())
    started = time.perf_counter()
    tts_result = tts_live.synthesize_current_response(tts_session.session_id)
    tts_elapsed = (time.perf_counter() - started) * 1000
    record(
        "TTS_DELAY",
        tts_result.status == "TEXT_ONLY" and tts_elapsed >= 50,
        "TEXT_ONLY_FALLBACK",
    )
    tts_live.close()

    with tempfile.TemporaryDirectory() as temporary:
        blocked_parent = Path(temporary) / "not-a-directory"
        blocked_parent.write_text("synthetic", encoding="utf-8")
        try:
            SQLitePersistence(blocked_parent / "demo.sqlite3").initialize()
            sqlite_failed = False
        except OSError:
            sqlite_failed = True
        record("SQLITE_FAILURE", sqlite_failed, "WRITE_BLOCKED")

    privacy = check_payload(
        NetworkScope.INTERNET,
        NetworkDestination.WEATHER_API,
        {"city_code": "310000", "raw_video": "synthetic-forbidden"},
    )
    record(
        "PRIVACY_REJECTION",
        privacy.decision is PrivacyDecision.REJECTED,
        "BLOCKED_BEFORE_CONNECTOR",
    )

    music_failure = Orchestrator()
    session = music_failure.start_session()
    session = music_failure.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
    session = music_failure.authorize(
        session.session_id, session.music_action.action_id, True, simulate_failure=True
    )
    music_result = session.results[next(iter(session.results))]
    record(
        "MUSIC_FAILURE",
        music_result.execution_status.value == "FAILED"
        and music_result.result["physical_action_performed"] is False,
        "FAILED_NO_PHYSICAL_ACTION",
    )

    ac_failure = Orchestrator()
    session = ac_failure.start_session()
    session = ac_failure.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
    session = ac_failure.authorize(session.session_id, session.music_action.action_id, False)
    session = ac_failure.authorize(
        session.session_id, session.ac_action.action_id, True, simulate_failure=True
    )
    ac_result = session.results[session.ac_action.action_id]
    record(
        "AC_MOCK_FAILURE",
        ac_result.execution_status.value == "FAILED"
        and ac_result.result["physical_action_performed"] is False,
        "FAILED_MOCK_ONLY",
    )

    expired_clock = FixedClock()
    expired_orchestrator = Orchestrator(clock=expired_clock)
    session = expired_orchestrator.start_session()
    session = expired_orchestrator.clarify(
        session.session_id, StateLabel.PHYSICAL_FATIGUE
    )
    expired_clock.value = session.music_action.expires_at
    session = expired_orchestrator.authorize(
        session.session_id, session.music_action.action_id, True
    )
    record(
        "ACTION_AUTHORIZATION_EXPIRED",
        session.authorizations[session.music_action.action_id].authorization_status
        is AuthorizationStatus.EXPIRED,
        "EXPIRED_NO_EXECUTION",
    )

    mismatch = Orchestrator()
    session = mismatch.start_session()
    session = mismatch.clarify(session.session_id, StateLabel.PHYSICAL_FATIGUE)
    try:
        mismatch.authorize(session.session_id, "music-wrong-id", True)
        mismatch_passed = False
    except ValueError:
        mismatch_passed = mismatch.music.executed_action_ids == []
    record("ACTION_ID_MISMATCH", mismatch_passed, "REJECTED_NO_EXECUTION")

    duplicate_backend = RecordingPlaybackBackend()
    with tempfile.TemporaryDirectory() as temporary:
        duplicate_player = LocalMusicPlayer(
            _synthetic_music_root(Path(temporary)), backend=duplicate_backend
        )
        proposal = _proposal(999)
        duplicate_player.execute(proposal, _approved(proposal), SYNTHETIC_NOW)
        try:
            duplicate_player.execute(proposal, _approved(proposal), SYNTHETIC_NOW)
            duplicate_passed = False
        except Exception:
            duplicate_passed = duplicate_backend.play_count == 1
        finally:
            duplicate_player.close()
    record("DUPLICATE_ACTION_EXECUTION", duplicate_passed, "REJECTED_AFTER_FIRST")
    return results


def run_recovery() -> RecoveryResult:
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "restart.sqlite3"
        base = [
            sys.executable,
            "-m",
            "scripts.phase5_restart_worker",
            "--database",
            str(database),
        ]
        subprocess.run(
            [*base, "--mode", "seed"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        recovered = subprocess.run(
            [*base, "--mode", "recover"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(recovered.stdout)
    return RecoveryResult.model_validate(payload)


async def build_report(sample_target: int) -> AcceptanceReport:
    benchmarks = await run_benchmarks(sample_target)
    e2e = [await run_fixed_e2e(5), await run_degraded_e2e(5)]
    faults = run_fault_matrix()
    recovery = [run_recovery()]
    overall_pass = (
        all(item.passed for item in benchmarks)
        and all(item.passed for item in e2e)
        and all(item.passed for item in faults)
        and all(item.passed for item in recovery)
    )
    return AcceptanceReport(
        schema_version=1,
        phase="Phase 5",
        generated_at=datetime.now(timezone.utc),
        sample_target=sample_target,
        benchmarks=benchmarks,
        e2e=e2e,
        faults=faults,
        recovery=recovery,
        model_restart_status="NOT_RUN_NO_AUTHORIZATION",
        overall_pass=overall_pass,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument(
        "--json", type=Path, default=ROOT / "reports" / "phase5" / "results.json"
    )
    parser.add_argument(
        "--markdown", type=Path, default=ROOT / "reports" / "phase5" / "REPORT.md"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.samples < 20:
        raise SystemExit("Phase 5 requires at least 20 samples per interface")
    report = asyncio.run(build_report(args.samples))
    json_text = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    markdown = render_markdown(report)
    assert_report_safe(json_text)
    assert_report_safe(markdown)
    write_report(report, args.json, args.markdown)
    print(
        json.dumps(
            {
                "overall_pass": report.overall_pass,
                "interfaces": len(report.benchmarks),
                "samples": sum(item.count for item in report.benchmarks),
                "e2e": [f"{item.success_count}/{item.count}" for item in report.e2e],
            },
            separators=(",", ":"),
        )
    )
    return 0 if report.overall_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
