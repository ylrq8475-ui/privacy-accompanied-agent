"""Pure-Mock orchestrator with optional Phase 1C SQLite persistence."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from threading import RLock
from typing import Any
from uuid import uuid4

from external_connector.contracts import ConnectorRejectedError
from external_connector.audius import (
    AudiusConnectorError,
    AudiusMusicConnector,
    AudiusPreview,
)
from external_connector.mock import MockConnectorTransportError, MockExternalConnector
from external_connector.weather import ExternalConnectorTransportError, RealExternalConnector

from backend.app.audit import InMemoryAuditLog, PersistentAuditLog
from backend.app.local_music import LocalMusicError, LocalMusicPlayer
from backend.app.mocks import (
    ASRMockError,
    ActionMockError,
    CameraMockError,
    MockAC,
    MockASR,
    MockMemory,
    MockMusic,
    MockStep3,
    MockVision,
    Step3MockTimeout,
)
from backend.app.policy import (
    ensure_action_executable,
    ensure_authorization_transition,
    evaluate_clarification,
    evaluate_reaction_suggestions,
)
from backend.app.persistence import SQLitePersistence
from backend.app.privacy_guard import PrivacyDecision, check_payload
from backend.app.schemas.actions import (
    ACActionPayload,
    ACMode,
    ActionAuthorization,
    ActionProposal,
    ActionResult,
    ActionType,
    AuthorizationStatus,
    ExecutionStatus,
    MusicActionPayload,
)
from backend.app.schemas.analysis import TextStateLabel, TextStateModelOutput
from backend.app.schemas.events import NetworkScope
from backend.app.schemas.network import MusicPayload, NetworkDestination, validate_connector_route
from backend.app.schemas.music import (
    PlaylistKey,
    logical_track_for_playlist,
    playlist_for_legacy_state,
    playlist_for_logical_track,
)
from backend.app.track_catalog import TrackCatalog, TrackCatalogError
from track_catalog.contracts import (
    CatalogLeaseRequest,
    CatalogResultRequest,
    CatalogSnapshotRequest,
)
from backend.app.schemas.phase4 import WeatherSnapshot, WeatherSource
from backend.app.schemas.persistence import (
    ActionOutcome,
    ConfidenceBand,
    EmotionHistoryContext,
    EmotionSummary,
    MemoryConfirmationRequest,
    MemoryRecord,
    PersistedAction,
    UserPreferenceConfirmation,
    UserPreferenceRecord,
    UserPreferencesResponse,
)
from backend.app.schemas.reaction import ACDecision, LLMReaction, PolicySuggestionDecision
from backend.app.schemas.step3 import StateLabel, Step3Output
from backend.app.schemas.visual import VisualPerceptionOutput
from backend.app.state_machine import DemoState, ensure_state_transition


Clock = Callable[[], datetime]

TEXT_CLARIFICATION_ANSWERS: dict[str, StateLabel] = {
    "主要是身体累": StateLabel.PHYSICAL_FATIGUE,
    "主要是心情低落": StateLabel.EMOTIONAL_LOW,
    "其他原因": StateLabel.OTHER,
}


class MockScenario(StrEnum):
    SUCCESS = "success"
    CAMERA_FAILED = "camera_failed"
    ASR_FAILED = "asr_failed"
    MODEL_TIMEOUT = "model_timeout"
    API_FAILED = "api_failed"
    PRIVACY_REJECTED = "privacy_rejected"


class SessionNotFound(KeyError):
    pass


class InvalidOperation(ValueError):
    pass


class LiveMusicExecutionError(ActionMockError):
    def __init__(self, fallback_reason: str, *, fetch_invoked: bool) -> None:
        super().__init__("live music execution failed")
        self.fallback_reason = fallback_reason
        self.fetch_invoked = fetch_invoked


@dataclass(slots=True)
class SessionState:
    session_id: str
    state: DemoState = DemoState.IDLE
    state_history: list[DemoState] = field(default_factory=lambda: [DemoState.IDLE])
    transcript: str | None = None
    step3_output: Step3Output | None = None
    clarification_answer: StateLabel | None = None
    music_action: ActionProposal | None = None
    ac_action: ActionProposal | None = None
    authorizations: dict[str, ActionAuthorization] = field(default_factory=dict)
    results: dict[str, ActionResult] = field(default_factory=dict)
    active: bool = True
    person_present: bool = False
    perception_source: str = "MOCK"
    interaction_source: str = "NONE"
    last_text_input: str | None = None
    runtime_mode: str = "MOCK"
    model_source: str = "MOCK_STEP3"
    degraded_reasons: list[str] = field(default_factory=list)
    component_health: dict[str, dict[str, Any]] = field(default_factory=dict)
    text_analysis: TextStateModelOutput | None = None
    provisional_reaction: LLMReaction | None = None
    final_reaction: LLMReaction | None = None
    confirmed_emotion: TextStateLabel | None = None
    weather_context: WeatherSnapshot | None = None
    ac_decision: ACDecision | None = None
    policy_suggestion_decisions: list[PolicySuggestionDecision] = field(
        default_factory=list
    )
    summary_saved: bool = False
    visual_perception: VisualPerceptionOutput | None = None
    visual_scene_id: str | None = None

    def snapshot(self, event_count: int, *, persistent: bool = False) -> dict[str, Any]:
        def dump(value: Any) -> Any:
            return value.model_dump(mode="json") if value is not None else None

        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "state_history": [item.value for item in self.state_history],
            "transcript": self.transcript,
            "step3_output": dump(self.step3_output),
            "clarification_answer": (
                self.clarification_answer.value if self.clarification_answer else None
            ),
            "music_action": dump(self.music_action),
            "ac_action": dump(self.ac_action),
            "authorizations": {
                action_id: authorization.model_dump(mode="json")
                for action_id, authorization in self.authorizations.items()
            },
            "results": {
                action_id: result.model_dump(mode="json")
                for action_id, result in self.results.items()
            },
            "event_count": event_count,
            "storage": "SQLITE_DEMO" if persistent else "IN_MEMORY_MOCK_ONLY",
            "active": self.active,
            "person_present": self.person_present,
            "perception_source": self.perception_source,
            "interaction_source": self.interaction_source,
            "last_text_input": self.last_text_input,
            "runtime_mode": self.runtime_mode,
            "model_source": self.model_source,
            "degraded_reasons": list(self.degraded_reasons),
            "component_health": self.component_health,
            "text_analysis": dump(self.text_analysis),
            "provisional_reaction": dump(self.provisional_reaction),
            "final_reaction": dump(self.final_reaction),
            "confirmed_emotion": (
                self.confirmed_emotion.value if self.confirmed_emotion else None
            ),
            "weather_context": dump(self.weather_context),
            "ac_decision": dump(self.ac_decision),
            "policy_suggestion_decisions": [
                item.model_dump(mode="json")
                for item in self.policy_suggestion_decisions
            ],
            "summary_saved": self.summary_saved,
            "visual_perception": dump(self.visual_perception),
            "visual_scene_id": self.visual_scene_id,
        }


class Orchestrator:
    """Coordinates only deterministic and synthetic Phase 1B components."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        audit_log: InMemoryAuditLog | None = None,
        connector: MockExternalConnector | None = None,
        live_connector: RealExternalConnector | MockExternalConnector | None = None,
        vision: MockVision | None = None,
        asr: MockASR | None = None,
        step3: MockStep3 | None = None,
        memory: MockMemory | None = None,
        music: MockMusic | None = None,
        live_music: LocalMusicPlayer | None = None,
        live_audius: AudiusMusicConnector | None = None,
        track_catalog: TrackCatalog | None = None,
        ac: MockAC | None = None,
        persistence: SQLitePersistence | None = None,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.persistence = persistence
        self.audit_log = audit_log or (
            PersistentAuditLog(persistence) if persistence is not None else InMemoryAuditLog()
        )
        self.connector = connector or MockExternalConnector()
        self.live_connector = live_connector or self.connector
        self.vision = vision or MockVision()
        self.asr = asr or MockASR()
        self.step3 = step3 or MockStep3()
        self.memory = memory or MockMemory()
        self.music = music or MockMusic()
        self.live_music = live_music
        self.live_audius = live_audius
        self.track_catalog = track_catalog
        self.ac = ac or MockAC()
        self.sessions: dict[str, SessionState] = {}
        self._lock = RLock()
        self._persistence_ready = False

    def start_session(
        self, scenario: MockScenario = MockScenario.SUCCESS, *, city_code: str = "310000"
    ) -> SessionState:
        with self._lock:
            self.initialize_persistence()
            session = SessionState(session_id=f"session-{uuid4().hex}")
            self.sessions[session.session_id] = session
            self.audit_log.record(
                session_id=session.session_id,
                event_type="SESSION_CREATED",
                payload={"initial_state": DemoState.IDLE.value, "mock": True},
                status="CREATED",
            )

            try:
                self.vision.detect_person(fail=scenario is MockScenario.CAMERA_FAILED)
            except CameraMockError:
                return self._transition(session, DemoState.CAMERA_FAILED, {"mock": True})
            session.person_present = True
            self._transition(session, DemoState.PERSON_DETECTED, {"mock": True})

            weather_payload: dict[str, Any] = {"city_code": city_code}
            if scenario is MockScenario.PRIVACY_REJECTED:
                weather_payload["context"] = {"rawAudio": "synthetic-forbidden-value"}
            if scenario is MockScenario.API_FAILED:
                self.connector.fail_next = True
            raw_request = {
                "request_id": f"request-{uuid4().hex}",
                "source_agent": "weather-agent",
                "destination": "WEATHER_API",
                "network_scope": "INTERNET",
                "payload": weather_payload,
                "created_at": self._now(),
            }
            try:
                weather_response = self.connector.send(raw_request)
            except ConnectorRejectedError:
                record = self.connector.audit_sink.records[-1]
                return self._transition(
                    session,
                    DemoState.PRIVACY_REJECTED,
                    {
                        "privacy_result": record.privacy_result,
                        "violation_paths": list(record.violation_paths),
                        "request_sent": False,
                    },
                )
            except MockConnectorTransportError:
                return self._transition(
                    session,
                    DemoState.API_FAILED,
                    {"mock": True, "request_sent_to_mock": True},
                )

            approved = self.connector.sent_requests[-1]
            self.audit_log.record(
                session_id=session.session_id,
                event_type="INTERNET_MOCK_REQUEST",
                payload={
                    "destination": approved.destination.value,
                    "outbound_payload": approved.payload,
                    "via_external_connector": True,
                    "mock": True,
                },
                status="SUCCEEDED",
                network_scope=NetworkScope.INTERNET,
                source_agent="external-connector",
                latency_ms=weather_response.latency_ms,
            )
            weather_body = weather_response.body if isinstance(weather_response.body, dict) else {}
            self._transition(
                session,
                DemoState.CONTEXT_READY,
                {"weather": weather_body, "mock": True},
            )
            self._transition(session, DemoState.LISTENING, {"mock": True})

            try:
                session.transcript = self.asr.transcribe(
                    fail=scenario is MockScenario.ASR_FAILED
                )
            except ASRMockError:
                return self._transition(session, DemoState.ASR_FAILED, {"mock": True})
            session.interaction_source = "MOCK_ASR"
            self._transition(session, DemoState.MOOD_ANALYSIS, {"mock": True})

            try:
                session.step3_output = self.step3.analyze(
                    fail=scenario is MockScenario.MODEL_TIMEOUT
                )
            except Step3MockTimeout:
                return self._transition(session, DemoState.MODEL_TIMEOUT, {"mock": True})

            return self._apply_policy(session)

    def begin_live_session(
        self,
        *,
        perception_source: str,
        degraded_reasons: list[str],
        visual_perception: VisualPerceptionOutput | None = None,
        visual_scene_id: str | None = None,
        wait_for_fixed_text: bool = False,
        city_code: str = "310000",
    ) -> SessionState:
        """Create a live-mode session after a confirmed person-appearance edge."""
        with self._lock:
            self.initialize_persistence()
            session = SessionState(
                session_id=f"session-{uuid4().hex}",
                runtime_mode="LIVE",
                perception_source=perception_source,
                model_source="NOT_CALLED",
                degraded_reasons=list(dict.fromkeys(degraded_reasons)),
                visual_perception=visual_perception,
                visual_scene_id=visual_scene_id,
            )
            self.sessions[session.session_id] = session
            self.audit_log.record(
                session_id=session.session_id,
                event_type="SESSION_CREATED",
                payload={"initial_state": DemoState.IDLE.value, "runtime_mode": "LIVE"},
                status="CREATED",
                source_agent="live-coordinator",
            )
            session.person_present = True
            self._transition(
                session,
                DemoState.PERSON_DETECTED,
                {"source": perception_source, "degraded": bool(degraded_reasons)},
            )

            weather = self._load_live_weather(session, city_code)
            self._transition(
                session,
                DemoState.CONTEXT_READY,
                {
                    "weather": weather,
                    "mock": weather.get("source") in {"MOCK", "FIXED_DEMO"},
                    "source": weather.get("source", "UNKNOWN"),
                },
            )
            if wait_for_fixed_text:
                session.interaction_source = "TEXT_FALLBACK_PENDING"
            return self._transition(
                session,
                DemoState.LISTENING,
                {
                    "input": (
                        "FIXED_TEXT"
                        if wait_for_fixed_text
                        else "STEPAUDIO_OR_TEXT_FALLBACK"
                    )
                },
            )

    def start_text_analysis_session(
        self,
        output: TextStateModelOutput,
        reaction: LLMReaction,
        weather: WeatherSnapshot,
    ) -> SessionState:
        """Enter the Demo interaction flow from a server-owned analysis result."""

        with self._lock:
            self.initialize_persistence()
            session = SessionState(
                session_id=f"session-{uuid4().hex}",
                runtime_mode="TEXT_ANALYSIS",
                perception_source="TEXT_ONLY",
                interaction_source="TEXT_ANALYSIS",
                model_source="STEP3",
                text_analysis=output,
                provisional_reaction=reaction,
                weather_context=weather,
            )
            self.sessions[session.session_id] = session
            self.audit_log.record(
                session_id=session.session_id,
                event_type="SESSION_CREATED",
                payload={
                    "initial_state": DemoState.IDLE.value,
                    "runtime_mode": "TEXT_ANALYSIS",
                    "raw_text_stored": False,
                    "provisional_reaction_available": True,
                },
                status="CREATED",
                source_agent="text-analysis-bridge",
            )
            self._transition(
                session,
                DemoState.MOOD_ANALYSIS,
                {
                    "model_source": "STEP3",
                    "candidate_labels": [
                        item.label.value for item in output.state_hypotheses
                    ],
                    "raw_text_stored": False,
                },
            )
            self.audit_log.record(
                session_id=session.session_id,
                event_type="STEP3_REACTION_GENERATED",
                payload={
                    "phase": "PROVISIONAL",
                    "tone": reaction.tone.value,
                    "suggestion_types": [item.type for item in reaction.suggestions],
                    "reply_persisted": False,
                    "raw_model_output_persisted": False,
                    "authorization_from_model": False,
                },
                status="SUCCEEDED",
                source_agent="step3-adapter",
                network_scope=NetworkScope.LOCAL,
            )
            self.audit_log.record(
                session_id=session.session_id,
                event_type="TEXT_WEATHER_CONTEXT",
                payload={
                    "weather_source": weather.source.value,
                    "outdoor_temperature_c": weather.temperature_c,
                    "outbound_payload": {"city_code": weather.city_code},
                    "via_external_connector": True,
                },
                status="SUCCEEDED",
                source_agent="external-connector",
                network_scope=NetworkScope.INTERNET,
            )
            self.audit_log.record(
                session_id=session.session_id,
                event_type="POLICY_CLARIFICATION_DECISION",
                payload={
                    "required": True,
                    "reason_codes": [
                        "TEXT_ANALYSIS_REQUIRES_EXPLICIT_CONFIRMATION"
                    ],
                    "model_prose_used_as_command": False,
                    "model_source": "STEP3",
                },
                status="SUCCEEDED",
                source_agent="policy-engine",
            )
            return self._transition(
                session,
                DemoState.CLARIFICATION_REQUIRED,
                {
                    "reason_codes": [
                        "TEXT_ANALYSIS_REQUIRES_EXPLICIT_CONFIRMATION"
                    ]
                },
            )

    def load_text_analysis_weather(self, city_code: str = "310000") -> WeatherSnapshot:
        """Fetch normalized outdoor context through the existing connector boundary."""

        with self._lock:
            self.initialize_persistence()
            temporary = SessionState(session_id=f"session-analysis-{uuid4().hex}")
            raw = self._load_live_weather(temporary, city_code)
            try:
                return WeatherSnapshot.model_validate_json(
                    json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
                )
            except ValueError:
                return WeatherSnapshot(
                    city_code=city_code,
                    temperature_c=float(raw.get("temperature_c", 22.0)),
                    condition=str(raw.get("condition", "clear")),
                    source=WeatherSource.FIXED_DEMO,
                    fetched_at=self._now(),
                    provider="FIXED_DEMO",
                )

    def reaction_memory_context(
        self,
    ) -> tuple[UserPreferencesResponse, EmotionHistoryContext]:
        if self.persistence is None:
            return (
                UserPreferencesResponse.model_validate(
                    {
                        "reply_style": {
                            "value": "GENTLE",
                            "confirmed": False,
                            "runtime_default": True,
                        },
                        "music_preference": {
                            "value": None,
                            "confirmed": False,
                            "runtime_default": False,
                        },
                    }
                ),
                EmotionHistoryContext(state_counts={}, recent_states=[]),
            )
        persistence = self._require_persistence()
        return persistence.get_user_preferences(), persistence.emotion_history_context()

    def confirm_text_state(
        self,
        session_id: str,
        label: TextStateLabel,
        reaction: LLMReaction,
    ) -> SessionState:
        """Apply a freshly generated final reaction after explicit nine-state confirmation."""

        with self._lock:
            session = self.get_session(session_id)
            self._ensure_session_active(session)
            if session.runtime_mode != "TEXT_ANALYSIS":
                raise InvalidOperation("state confirmation is only available for text analysis")
            if session.state is not DemoState.CLARIFICATION_REQUIRED:
                raise InvalidOperation("session is not waiting for state confirmation")
            if not isinstance(label, TextStateLabel):
                raise InvalidOperation("confirmation label must be a TextStateLabel")
            session.confirmed_emotion = label
            session.final_reaction = reaction
            self.audit_log.record(
                session_id=session.session_id,
                event_type="STEP3_REACTION_GENERATED",
                payload={
                    "phase": "FINAL",
                    "confirmed_emotion": label.value,
                    "tone": reaction.tone.value,
                    "suggestion_types": [item.type for item in reaction.suggestions],
                    "reply_persisted": False,
                    "raw_model_output_persisted": False,
                    "authorization_from_model": False,
                },
                status="SUCCEEDED",
                source_agent="step3-adapter",
                network_scope=NetworkScope.LOCAL,
            )
            self._transition(
                session,
                DemoState.USER_CLARIFIED,
                {"answer": label.value, "nine_state_confirmation": True},
            )
            return self._prepare_reaction_actions(session)

    def _load_live_weather(
        self, session: SessionState, city_code: str = "310000"
    ) -> dict[str, Any]:
        now = self._now()
        raw_request = {
            "request_id": f"request-{uuid4().hex}",
            "source_agent": "weather-agent",
            "destination": "WEATHER_API",
            "network_scope": "INTERNET",
            "payload": {"city_code": city_code},
            "created_at": now,
        }
        try:
            response = self.live_connector.send(raw_request)
            approved = self.live_connector.sent_requests[-1]
            body = response.body if isinstance(response.body, dict) else {}
            if body.get("mock") is True:
                self.audit_log.record(
                    session_id=session.session_id,
                    event_type="INTERNET_MOCK_REQUEST",
                    payload={
                        "destination": approved.destination.value,
                        "outbound_payload": approved.payload,
                        "via_external_connector": True,
                        "mock": True,
                    },
                    status="SUCCEEDED",
                    network_scope=NetworkScope.INTERNET,
                    source_agent="external-connector",
                    latency_ms=response.latency_ms,
                )
                return {**body, "source": "MOCK"}

            snapshot = WeatherSnapshot.model_validate_json(
                json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            )
            if snapshot.source is not WeatherSource.REAL_API:
                raise ExternalConnectorTransportError("real weather source was not REAL_API")
            if self.persistence is not None:
                self.persistence.save_weather_cache(snapshot)
            self.audit_log.record(
                session_id=session.session_id,
                event_type="INTERNET_REQUEST",
                payload={
                    "destination": approved.destination.value,
                    "outbound_payload": approved.payload,
                    "via_external_connector": True,
                    "privacy_result": PrivacyDecision.ALLOWED.value,
                    "weather_source": snapshot.source.value,
                },
                status="SUCCEEDED",
                network_scope=NetworkScope.INTERNET,
                source_agent="external-connector",
                latency_ms=response.latency_ms,
            )
            return snapshot.model_dump(mode="json")
        except ConnectorRejectedError:
            record = self.live_connector.audit_sink.records[-1]
            self.audit_log.record(
                session_id=session.session_id,
                event_type="INTERNET_REQUEST_REJECTED",
                payload={
                    "destination": "WEATHER_API",
                    "request_sent": False,
                    "privacy_result": record.privacy_result,
                    "violation_paths": list(record.violation_paths),
                },
                status="REJECTED",
                network_scope=NetworkScope.INTERNET,
                source_agent="privacy-guard",
            )
            session.degraded_reasons.append("WEATHER_PRIVACY_REJECTED")
        except (ExternalConnectorTransportError, MockConnectorTransportError, ValueError):
            approved = (
                self.live_connector.sent_requests[-1]
                if self.live_connector.sent_requests
                else None
            )
            self.audit_log.record(
                session_id=session.session_id,
                event_type="INTERNET_REQUEST",
                payload={
                    "destination": "WEATHER_API",
                    "outbound_payload": (
                        approved.payload if approved is not None else {"city_code": city_code}
                    ),
                    "via_external_connector": True,
                    "privacy_result": PrivacyDecision.ALLOWED.value,
                    "weather_source": "UNAVAILABLE",
                },
                status="FAILED",
                network_scope=NetworkScope.INTERNET,
                source_agent="external-connector",
            )
            session.degraded_reasons.append("WEATHER_API_FAILED")

        if self.persistence is not None:
            cached = self.persistence.get_weather_cache(city_code)
            if cached is not None:
                if "WEATHER_CACHE_FALLBACK" not in session.degraded_reasons:
                    session.degraded_reasons.append("WEATHER_CACHE_FALLBACK")
                return cached.model_dump(mode="json")
        if "WEATHER_FIXED_DEMO_FALLBACK" not in session.degraded_reasons:
            session.degraded_reasons.append("WEATHER_FIXED_DEMO_FALLBACK")
        return WeatherSnapshot(
            city_code=city_code,
            temperature_c=22.0,
            condition="clear",
            source=WeatherSource.FIXED_DEMO,
            fetched_at=now,
            provider="FIXED_DEMO",
        ).model_dump(mode="json")

    def mark_live_asr_fallback(self, session_id: str, reason: str) -> SessionState:
        with self._lock:
            session = self.get_session(session_id)
            self._ensure_session_active(session)
            if session.runtime_mode != "LIVE" or session.state is not DemoState.LISTENING:
                raise InvalidOperation("live session is not waiting for ASR fallback")
            if reason not in session.degraded_reasons:
                session.degraded_reasons.append(reason)
            session.interaction_source = "TEXT_FALLBACK_PENDING"
            return session

    def continue_live_pipeline(
        self,
        session_id: str,
        *,
        transcript: str,
        interaction_source: str,
        step3_output: Step3Output,
        model_source: str,
        degraded_reason: str | None = None,
    ) -> SessionState:
        with self._lock:
            session = self.get_session(session_id)
            self._ensure_session_active(session)
            if session.runtime_mode != "LIVE" or session.state is not DemoState.LISTENING:
                raise InvalidOperation("live session is not waiting for a transcript")
            session.transcript = transcript
            session.interaction_source = interaction_source
            self._transition(session, DemoState.MOOD_ANALYSIS, {"source": interaction_source})
            session.step3_output = step3_output
            session.model_source = model_source
            if degraded_reason and degraded_reason not in session.degraded_reasons:
                session.degraded_reasons.append(degraded_reason)
            return self._apply_policy(session)

    def clarify(self, session_id: str, answer: StateLabel) -> SessionState:
        with self._lock:
            session = self.get_session(session_id)
            self._ensure_session_active(session)
            if session.state is not DemoState.CLARIFICATION_REQUIRED:
                raise InvalidOperation("session is not waiting for clarification")
            if session.runtime_mode == "TEXT_ANALYSIS":
                raise InvalidOperation(
                    "text analysis requires the nine-state confirmation endpoint"
                )
            if not isinstance(answer, StateLabel):
                raise InvalidOperation("clarification answer must be a StateLabel")
            session.clarification_answer = answer
            self._transition(
                session,
                DemoState.USER_CLARIFIED,
                {"answer": answer.value, "mock": session.runtime_mode == "MOCK"},
            )
            return self._prepare_actions(session)

    def submit_text(self, session_id: str, text: str) -> SessionState:
        with self._lock:
            session = self.get_session(session_id)
            self._ensure_session_active(session)
            if session.state is not DemoState.CLARIFICATION_REQUIRED:
                raise InvalidOperation("session is not waiting for clarification text")
            try:
                answer = TEXT_CLARIFICATION_ANSWERS[text]
            except KeyError as error:
                raise InvalidOperation("text is not an allowlisted Demo answer") from error
            session.last_text_input = text
            session.interaction_source = "TEXT"
            self.audit_log.record(
                session_id=session.session_id,
                event_type="USER_TEXT_INPUT",
                payload={"text": text, "input_source": "TEXT", "mock": True},
                status="ACCEPTED",
                source_agent="text-input",
            )
            return self.clarify(session_id, answer)

    def reset_session(self, session_id: str) -> SessionState:
        with self._lock:
            session = self.get_session(session_id)
            if not session.active:
                return session
            if session.person_present:
                session.person_present = False
                self.audit_log.record(
                    session_id=session.session_id,
                    event_type="PERSON_LEFT",
                    payload={
                        "person_present": False,
                        "input_source": session.perception_source,
                        "mock": session.runtime_mode == "MOCK",
                    },
                    status="OBSERVED",
                    source_agent=(
                        "vision-adapter"
                        if session.runtime_mode == "LIVE"
                        else "vision-mock"
                    ),
                )
            else:
                self.audit_log.record(
                    session_id=session.session_id,
                    event_type="TEXT_SESSION_CLOSED",
                    payload={"person_event_claimed": False},
                    status="OBSERVED",
                    source_agent="text-analysis-bridge",
                )
            if session.runtime_mode == "TEXT_ANALYSIS":
                self._save_summary_once(session)
            session.active = False
            self.audit_log.record(
                session_id=session.session_id,
                event_type="DEMO_RESET",
                payload={
                    "active": False,
                    "pending_actions_executed": False,
                    "memory_deleted": False,
                    "mock": True,
                },
                status="RESET",
                source_agent="orchestrator",
            )
            return session

    def authorize(
        self,
        session_id: str,
        action_id: str,
        approved: bool,
        *,
        simulate_failure: bool = False,
    ) -> SessionState:
        with self._lock:
            session = self.get_session(session_id)
            self._ensure_session_active(session)
            proposal = self._expected_proposal(session)
            if proposal is None or proposal.action_id != action_id:
                self.audit_log.record(
                    session_id=session.session_id,
                    event_type="AUTHORIZATION_REJECTED",
                    payload={"reason": "ACTION_ID_MISMATCH"},
                    status="REJECTED",
                    action_id=action_id if action_id.startswith(("music-", "ac-")) else None,
                    source_agent="authorization-manager",
                )
                raise InvalidOperation("authorization action_id does not match pending action")

            now = self._now()
            previous = session.authorizations[proposal.action_id]
            if now >= proposal.expires_at or now >= previous.expires_at:
                session.authorizations[proposal.action_id] = ActionAuthorization(
                    action_id=proposal.action_id,
                    action_type=proposal.action_type,
                    authorization_status=AuthorizationStatus.EXPIRED,
                    decided_at=now,
                    expires_at=proposal.expires_at,
                )
                self._persist_authorization(
                    session, proposal.action_id, AuthorizationStatus.EXPIRED, now
                )
                self._transition(
                    session,
                    DemoState.ACTION_EXPIRED,
                    {"reason": "AUTHORIZATION_EXPIRED"},
                    action_id=proposal.action_id,
                )
                return self._continue_after_terminal_action(session, proposal.action_type)

            candidate = ActionAuthorization(
                action_id=proposal.action_id,
                action_type=proposal.action_type,
                authorization_status=(
                    AuthorizationStatus.APPROVED if approved else AuthorizationStatus.REJECTED
                ),
                decided_at=now,
                expires_at=proposal.expires_at,
            )
            session.authorizations[proposal.action_id] = ensure_authorization_transition(
                previous, candidate
            )
            self._persist_authorization(
                session, proposal.action_id, candidate.authorization_status, now
            )
            if not approved:
                self._transition(
                    session,
                    DemoState.ACTION_REJECTED,
                    {"reason": "USER_REJECTED"},
                    action_id=proposal.action_id,
                )
                return self._continue_after_terminal_action(session, proposal.action_type)

            ensure_action_executable(proposal, candidate, now)
            if proposal.action_type is ActionType.PLAY_MUSIC:
                self._transition(
                    session,
                    DemoState.MUSIC_AUTHORIZED,
                    {"authorization": "APPROVED"},
                    action_id=proposal.action_id,
                )
                self._persist_running(session, proposal.action_id)
                use_local_player = (
                    session.runtime_mode == "LIVE" and self.live_music is not None
                )
                try:
                    if use_local_player:
                        result = self._execute_live_music(
                            session,
                            proposal,
                            candidate,
                            now,
                            simulate_failure=simulate_failure,
                        )
                    else:
                        self.music.fail_next = simulate_failure
                        result = self.music.execute(proposal, now)
                except ActionMockError as error:
                    if isinstance(error, LiveMusicExecutionError):
                        result = self._failed_live_music_result(
                            proposal,
                            now,
                            fallback_reason=error.fallback_reason,
                            fetch_invoked=error.fetch_invoked,
                        )
                    else:
                        result = self._failed_action_result(
                            proposal,
                            now,
                            mock=not use_local_player,
                            code=(
                                "LOCAL_PLAYBACK_FAILED"
                                if use_local_player
                                else "MOCK_EXECUTION_FAILED"
                            ),
                        )
                    session.results[proposal.action_id] = result
                    self._persist_result(session, result)
                    self._transition(
                        session,
                        DemoState.ACTION_FAILED,
                        result.result,
                        action_id=proposal.action_id,
                    )
                    return self._continue_after_terminal_action(session, proposal.action_type)
                session.results[proposal.action_id] = result
                self._persist_result(session, result)
                if use_local_player:
                    self._record_action_result(
                        session, result, NetworkScope.LOCAL, "local-music-player"
                    )
                else:
                    self._record_mock_result(session, result, NetworkScope.LOCAL)
                self._transition(
                    session,
                    DemoState.MUSIC_EXECUTED,
                    result.result,
                    action_id=proposal.action_id,
                )
                return self._continue_after_terminal_action(
                    session, proposal.action_type
                )

            self._transition(
                session,
                DemoState.AC_AUTHORIZED,
                {"authorization": "APPROVED"},
                action_id=proposal.action_id,
            )
            self._persist_running(session, proposal.action_id)
            self.ac.fail_next = simulate_failure
            try:
                result = self.ac.execute(proposal, now)
            except ActionMockError:
                result = self._failed_mock_result(proposal, now)
                session.results[proposal.action_id] = result
                self._persist_result(session, result)
                self._transition(
                    session,
                    DemoState.ACTION_FAILED,
                    {"mock": True, "physical_action_performed": False},
                    action_id=proposal.action_id,
                )
                return self._continue_after_terminal_action(session, proposal.action_type)
            session.results[proposal.action_id] = result
            self._persist_result(session, result)
            self._record_mock_result(session, result, NetworkScope.LOCAL)
            self._transition(
                session,
                DemoState.AC_EXECUTED,
                result.result,
                action_id=proposal.action_id,
            )
            return self._transition(session, DemoState.COMPLETED, {"mock": True})

    def get_session(self, session_id: str) -> SessionState:
        try:
            return self.sessions[session_id]
        except KeyError as error:
            raise SessionNotFound(session_id) from error

    def snapshot(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        return session.snapshot(
            len(self.audit_log.list_events(session_id)),
            persistent=self.persistence is not None,
        )

    def _execute_live_music(
        self,
        session: SessionState,
        proposal: ActionProposal,
        authorization: ActionAuthorization,
        now: datetime,
        *,
        simulate_failure: bool,
    ) -> ActionResult:
        if self.live_music is None:
            raise LocalMusicError("live music player is unavailable")

        fallback_reason = "NOT_CONFIGURED"
        fetch_invoked = False
        preview: AudiusPreview | None = None
        playlist_key: PlaylistKey | None = None
        lease = None
        try:
            playlist_key = playlist_for_logical_track(proposal.payload.track_id)
        except ValueError:
            fallback_reason = "LOGICAL_TRACK_NOT_ALLOWLISTED"

        if (
            playlist_key is not None
            and self.live_audius is not None
            and self.track_catalog is not None
            and self.live_audius.settings.preview_ready
        ):
            can_lease = False
            try:
                category_status = self.track_catalog.category_status(playlist_key)
                can_lease = category_status != "EMPTY"
                self._record_catalog_event(
                    session,
                    proposal,
                    "CATALOG_HEALTH",
                    {"playlist_key": playlist_key.value, "status": category_status},
                )
            except TrackCatalogError as error:
                fallback_reason = error.code
                category_status = "UNAVAILABLE"
            if (
                category_status in {"EMPTY", "STALE"}
                and self.live_audius.settings.configured_for(playlist_key)
            ):
                sync_request = self._music_internet_request(
                    now,
                    {
                        "action": "sync_playlist",
                        "playlist_ref": playlist_key.value,
                    },
                )
                try:
                    snapshot = self.live_audius.sync_playlist(
                        sync_request, playlist_key
                    )
                    fetch_invoked = True
                    stored = self.track_catalog.replace_snapshot(
                        CatalogSnapshotRequest(
                            playlist_key=playlist_key,
                            playlist_id=snapshot.provider_playlist_id,
                            track_ids=list(snapshot.track_ids),
                            source_count=snapshot.source_count,
                            truncated=snapshot.truncated,
                        )
                    )
                    can_lease = True
                    self.audit_log.record(
                        session_id=session.session_id,
                        event_type="AUDIUS_PLAYLIST_SYNC",
                        payload={
                            "destination": NetworkDestination.PUBLIC_MUSIC_API.value,
                            "outbound_payload": sync_request["payload"],
                            "via_external_connector": True,
                            "privacy_result": PrivacyDecision.ALLOWED.value,
                            "provider": "AUDIUS",
                            "provider_playlist_id": snapshot.provider_playlist_id,
                            "playlist_key": playlist_key.value,
                            "track_count": stored.track_count,
                            "source_count": stored.source_count,
                            "truncated": stored.truncated,
                            "revision": stored.revision,
                            "request_sent": True,
                            "url_persisted": False,
                        },
                        status="SUCCEEDED",
                        action_id=proposal.action_id,
                        network_scope=NetworkScope.INTERNET,
                        source_agent="external-connector",
                        latency_ms=snapshot.latency_ms,
                    )
                except ConnectorRejectedError:
                    fallback_reason = "AUDIUS_PRIVACY_REJECTED"
                    self._record_privacy_rejection(session, proposal)
                except AudiusConnectorError as error:
                    fallback_reason = error.code
                    fetch_invoked = fetch_invoked or error.request_sent
                    self._record_audius_failure(
                        session,
                        proposal,
                        sync_request["payload"],
                        error,
                        provider_track_id=None,
                    )
                except TrackCatalogError as error:
                    fallback_reason = error.code
                    self._record_catalog_event(
                        session,
                        proposal,
                        "CATALOG_SNAPSHOT_FAILED",
                        {"playlist_key": playlist_key.value, "reason": error.code},
                        status="DEGRADED",
                    )

            if can_lease:
                try:
                    lease = self.track_catalog.lease(
                        CatalogLeaseRequest(
                            action_id=proposal.action_id,
                            playlist_key=playlist_key,
                            logical_track_id=proposal.payload.track_id,
                        )
                    )
                    self._record_catalog_event(
                        session,
                        proposal,
                        "CATALOG_TRACK_LEASED",
                        {
                            "playlist_key": playlist_key.value,
                            "provider_track_id": lease.provider_track_id,
                            "revision": lease.revision,
                            "stale": lease.stale,
                            "repeated": lease.repeated,
                        },
                    )
                except TrackCatalogError as error:
                    fallback_reason = error.code

            if lease is not None:
                raw_request = self._music_internet_request(
                    now,
                    {"action": "play", "track_id": proposal.payload.track_id},
                )
                try:
                    preview = self.live_audius.fetch_preview(
                        raw_request, lease.provider_track_id
                    )
                except ConnectorRejectedError:
                    fallback_reason = "AUDIUS_PRIVACY_REJECTED"
                    self._record_privacy_rejection(session, proposal)
                except AudiusConnectorError as error:
                    fallback_reason = error.code
                    fetch_invoked = fetch_invoked or error.request_sent
                    self._record_audius_failure(
                        session,
                        proposal,
                        raw_request["payload"],
                        error,
                        provider_track_id=lease.provider_track_id,
                    )
                    self._record_catalog_fetch_result(
                        proposal,
                        playlist_key,
                        lease.provider_track_id,
                        succeeded=False,
                        reason_code=error.code,
                    )
                else:
                    fetch_invoked = True
                    self._record_catalog_fetch_result(
                        proposal,
                        playlist_key,
                        lease.provider_track_id,
                        succeeded=True,
                        reason_code=None,
                    )
                    self.audit_log.record(
                        session_id=session.session_id,
                        event_type="INTERNET_REQUEST",
                        payload={
                            "destination": NetworkDestination.PUBLIC_MUSIC_API.value,
                            "outbound_payload": raw_request["payload"],
                            "via_external_connector": True,
                            "privacy_result": PrivacyDecision.ALLOWED.value,
                            "provider": "AUDIUS",
                            "provider_track_id": preview.provider_track_id,
                            "playlist_key": playlist_key.value,
                            "catalog_revision": lease.revision,
                            "catalog_stale": lease.stale,
                            "preview": True,
                            "request_sent": True,
                            "status": "READY",
                            "size_bytes": preview.size_bytes,
                        },
                        status="SUCCEEDED",
                        action_id=proposal.action_id,
                        network_scope=NetworkScope.INTERNET,
                        source_agent="external-connector",
                        latency_ms=preview.latency_ms,
                    )
        elif playlist_key is not None and self.live_audius is not None:
            fallback_reason = (
                "PLAYLIST_NOT_CONFIGURED"
                if self.live_audius.settings.configured
                else "NOT_CONFIGURED"
            )

        if preview is not None:
            self._record_local_music_request(
                session,
                proposal,
                source="AUDIUS_PREVIEW",
                fallback_reason=None,
            )
            try:
                if simulate_failure:
                    raise LocalMusicError("synthetic preview playback failure")
                result = self.live_music.execute_preview(
                    proposal,
                    authorization,
                    now,
                    audio=preview.audio,
                    provider_track_id=preview.provider_track_id,
                    size_bytes=preview.size_bytes,
                    fetch_latency_ms=preview.latency_ms,
                )
                if lease is not None:
                    result.result.update(
                        {
                            "playlist_key": lease.playlist_key.value,
                            "catalog_revision": lease.revision,
                            "catalog_stale": lease.stale,
                        }
                    )
                return result
            except LocalMusicError:
                fallback_reason = "AUDIUS_PLAYBACK_FAILED"

        self._record_local_music_request(
            session,
            proposal,
            source="LOCAL_FALLBACK",
            fallback_reason=fallback_reason,
        )
        try:
            if simulate_failure:
                raise LocalMusicError("synthetic local playback failure")
            result = self.live_music.execute(
                proposal,
                authorization,
                now,
                fallback_reason=fallback_reason,
                fetch_invoked=fetch_invoked,
            )
            if playlist_key is not None:
                result.result["playlist_key"] = playlist_key.value
            if lease is not None:
                result.result.update(
                    {
                        "catalog_revision": lease.revision,
                        "catalog_stale": lease.stale,
                    }
                )
            return result
        except LocalMusicError as error:
            raise LiveMusicExecutionError(
                fallback_reason, fetch_invoked=fetch_invoked
            ) from error

    @staticmethod
    def _music_internet_request(
        now: datetime, payload: dict[str, str]
    ) -> dict[str, object]:
        return {
            "request_id": f"request-{uuid4().hex}",
            "source_agent": "music-agent",
            "destination": "PUBLIC_MUSIC_API",
            "network_scope": "INTERNET",
            "payload": payload,
            "created_at": now,
        }

    def _record_privacy_rejection(
        self, session: SessionState, proposal: ActionProposal
    ) -> None:
        if self.live_audius is None or not self.live_audius.audit_sink.records:
            return
        record = self.live_audius.audit_sink.records[-1]
        self.audit_log.record(
            session_id=session.session_id,
            event_type="INTERNET_REQUEST_REJECTED",
            payload={
                "destination": NetworkDestination.PUBLIC_MUSIC_API.value,
                "request_sent": False,
                "privacy_result": record.privacy_result,
                "violation_paths": list(record.violation_paths),
            },
            status="REJECTED",
            action_id=proposal.action_id,
            network_scope=NetworkScope.INTERNET,
            source_agent="privacy-guard",
        )

    def _record_audius_failure(
        self,
        session: SessionState,
        proposal: ActionProposal,
        outbound_payload: object,
        error: AudiusConnectorError,
        *,
        provider_track_id: str | None,
    ) -> None:
        health = self.live_audius.health() if self.live_audius is not None else {}
        self.audit_log.record(
            session_id=session.session_id,
            event_type="INTERNET_REQUEST",
            payload={
                "destination": NetworkDestination.PUBLIC_MUSIC_API.value,
                "outbound_payload": outbound_payload,
                "via_external_connector": True,
                "privacy_result": PrivacyDecision.ALLOWED.value,
                "provider": "AUDIUS",
                "provider_track_id": provider_track_id,
                "preview": provider_track_id is not None,
                "request_sent": error.request_sent,
                "status": "DEGRADED",
                "size_bytes": 0,
                "fallback_reason": error.code,
            },
            status="DEGRADED",
            action_id=proposal.action_id,
            network_scope=NetworkScope.INTERNET,
            source_agent="external-connector",
            latency_ms=int(health.get("latency_ms", 0)),
        )

    def _record_catalog_event(
        self,
        session: SessionState,
        proposal: ActionProposal,
        event_type: str,
        payload: dict[str, object],
        *,
        status: str = "SUCCEEDED",
    ) -> None:
        self.audit_log.record(
            session_id=session.session_id,
            event_type=event_type,
            payload={
                **payload,
                "destination": "AUDIUS_CATALOG",
                "via_external_connector": False,
            },
            status=status,
            action_id=proposal.action_id,
            network_scope=NetworkScope.LOCAL,
            source_agent="track-catalog-client",
        )

    def _record_catalog_fetch_result(
        self,
        proposal: ActionProposal,
        playlist_key: PlaylistKey,
        provider_track_id: str,
        *,
        succeeded: bool,
        reason_code: str | None,
    ) -> None:
        if self.track_catalog is None:
            return
        try:
            self.track_catalog.record_result(
                CatalogResultRequest(
                    action_id=proposal.action_id,
                    playlist_key=playlist_key,
                    provider_track_id=provider_track_id,
                    outcome=("FETCH_SUCCEEDED" if succeeded else "FETCH_FAILED"),
                    reason_code=reason_code,
                )
            )
        except TrackCatalogError:
            pass

    def _record_local_music_request(
        self,
        session: SessionState,
        proposal: ActionProposal,
        *,
        source: str,
        fallback_reason: str | None,
    ) -> None:
        command = MusicPayload(action="play", track_id=proposal.payload.track_id)
        privacy = check_payload(
            NetworkScope.LOCAL,
            NetworkDestination.LOCAL_MUSIC,
            command.model_dump(mode="json"),
        )
        if privacy.decision is not PrivacyDecision.ALLOWED:
            raise LocalMusicError("local music payload was rejected")
        validate_connector_route(NetworkScope.LOCAL, False)
        self.audit_log.record(
            session_id=session.session_id,
            event_type="LOCAL_ACTION_REQUEST",
            payload={
                "destination": NetworkDestination.LOCAL_MUSIC.value,
                "outbound_payload": privacy.allowed_payload or {},
                "via_external_connector": False,
                "privacy_result": privacy.decision.value,
                "source": source,
                "playback_scope": "LOCAL",
                "fallback_reason": fallback_reason,
                "audio_bytes_persisted": False,
            },
            status="DISPATCHED",
            action_id=proposal.action_id,
            network_scope=NetworkScope.LOCAL,
            source_agent="local-music-adapter",
        )

    def phase4_health(self) -> list[dict[str, object]]:
        weather_real = bool(getattr(self.live_connector, "is_real", False))
        weather = {
            "component": "WEATHER_EGRESS",
            "available": weather_real,
            "status": "CONFIGURED_NOT_CALLED" if weather_real else "MOCK_ONLY",
            "latency_ms": 0,
        }
        if self.live_music is None:
            music = {
                "component": "LOCAL_MUSIC",
                "available": False,
                "status": "MOCK_ONLY",
                "latency_ms": 0,
            }
        else:
            music = self.live_music.health()
        audius = (
            self.live_audius.health()
            if self.live_audius is not None
            else {
                "component": "AUDIUS_MUSIC",
                "available": False,
                "status": "NOT_CONFIGURED",
                "latency_ms": 0,
            }
        )
        if self.track_catalog is None:
            catalog = {
                "component": "AUDIUS_CATALOG",
                "available": False,
                "status": "UNAVAILABLE",
                "latency_ms": 0,
                "network_scope": "LOCAL",
            }
        else:
            try:
                catalog = self.track_catalog.health()
            except TrackCatalogError:
                catalog = {
                    "component": "AUDIUS_CATALOG",
                    "available": False,
                    "status": "UNAVAILABLE",
                    "latency_ms": 0,
                    "network_scope": "LOCAL",
                }
            else:
                categories = catalog.get("categories")
                configured = (
                    set(self.live_audius.settings.playlist_urls)
                    if self.live_audius is not None
                    else set()
                )
                if isinstance(categories, dict):
                    for playlist_key in PlaylistKey:
                        item = categories.get(playlist_key.value)
                        if isinstance(item, dict):
                            item["online_sync_configured"] = (
                                playlist_key in configured
                            )
                    configured_statuses = {
                        str(categories[item.value].get("status", "EMPTY"))
                        for item in configured
                        if isinstance(categories.get(item.value), dict)
                    }
                    if configured_statuses == {"READY"}:
                        catalog["status"] = "READY"
                    elif "DEGRADED" in configured_statuses:
                        catalog["status"] = "DEGRADED"
                    elif "STALE" in configured_statuses:
                        catalog["status"] = "STALE"
                    elif configured_statuses:
                        catalog["status"] = "CONFIGURED_NOT_PROBED"
        return [weather, music, audius, catalog]

    def close(self) -> None:
        if self.live_music is not None:
            self.live_music.close()

    def initialize_persistence(self) -> None:
        if self.persistence is None or self._persistence_ready:
            return
        with self._lock:
            if self._persistence_ready:
                return
            self.persistence.initialize()
            self.persistence.recover_actions(self._now())
            self._persistence_ready = True

    def confirm_memory(self, request: MemoryConfirmationRequest) -> MemoryRecord:
        persistence = self._require_persistence()
        record = persistence.confirm_memory(request, self._now())
        self.audit_log.record(
            session_id="session-memory-api",
            event_type="MEMORY_CONFIRMED",
            payload={"memory_id": record.memory_id, "confirmed": True},
            status="SUCCEEDED",
            source_agent="memory-api",
        )
        return record

    def list_memories(self) -> list[MemoryRecord]:
        return self._require_persistence().list_memories()

    def delete_memory(self, memory_id: str) -> bool:
        deleted = self._require_persistence().delete_memory(memory_id)
        if deleted:
            self.audit_log.record(
                session_id="session-memory-api",
                event_type="MEMORY_DELETED",
                payload={"memory_id": memory_id},
                status="SUCCEEDED",
                source_agent="memory-api",
            )
        return deleted

    def get_user_preferences(self) -> UserPreferencesResponse:
        return self._require_persistence().get_user_preferences()

    def confirm_user_preference(
        self, request: UserPreferenceConfirmation
    ) -> UserPreferenceRecord:
        record = self._require_persistence().confirm_user_preference(
            request, self._now()
        )
        self.audit_log.record(
            session_id="session-preference-api",
            event_type="USER_PREFERENCE_CONFIRMED",
            payload={"key": record.key, "confirmed": True},
            status="SUCCEEDED",
            source_agent="preference-api",
        )
        return record

    def delete_user_preference(self, key: str) -> bool:
        deleted = self._require_persistence().delete_user_preference(key)
        if deleted:
            self.audit_log.record(
                session_id="session-preference-api",
                event_type="USER_PREFERENCE_DELETED",
                payload={"key": key},
                status="SUCCEEDED",
                source_agent="preference-api",
            )
        return deleted

    def list_emotion_summaries(self) -> list[EmotionSummary]:
        return self._require_persistence().list_emotion_summaries()

    def clear_emotion_summaries(self) -> int:
        count = self._require_persistence().clear_emotion_summaries()
        self.audit_log.record(
            session_id="session-summary-api",
            event_type="EMOTION_SUMMARIES_CLEARED",
            payload={"deleted_count": count},
            status="SUCCEEDED",
            source_agent="summary-api",
        )
        return count

    def get_persisted_action(self, action_id: str) -> PersistedAction:
        return self._require_persistence().get_action(action_id)

    def _prepare_reaction_actions(self, session: SessionState) -> SessionState:
        if (
            session.final_reaction is None
            or session.weather_context is None
            or session.confirmed_emotion is None
        ):
            raise InvalidOperation("final reaction, weather and confirmed emotion are required")
        preferences, _ = self.reaction_memory_context()
        self._transition(
            session,
            DemoState.MEMORY_RETRIEVED,
            {
                "reply_style_confirmed": preferences.reply_style.confirmed,
                "music_preference_confirmed": preferences.music_preference.confirmed,
                "history_is_aggregate_only": True,
            },
        )
        policy = evaluate_reaction_suggestions(
            session.final_reaction,
            session.weather_context,
            music_preference=preferences.music_preference.value,
            music_preference_confirmed=preferences.music_preference.confirmed,
            confirmed_emotion=session.confirmed_emotion,
        )
        session.ac_decision = policy.ac_decision
        session.policy_suggestion_decisions = list(policy.decisions)
        self.audit_log.record(
            session_id=session.session_id,
            event_type="AC_DECISION_COMPUTED",
            payload={
                **policy.ac_decision.model_dump(mode="json"),
                "model_suggestion_used": False,
                "raw_text_stored": False,
            },
            status="ACTION_PROPOSED" if policy.ac_payload is not None else "NO_ACTION",
            source_agent="policy-engine",
        )
        for decision in policy.decisions:
            self.audit_log.record(
                session_id=session.session_id,
                event_type="REACTION_SUGGESTION_POLICY",
                payload=decision.model_dump(mode="json"),
                status="ACCEPTED" if decision.accepted else "REJECTED",
                source_agent="policy-engine",
            )

        now = self._now()
        expires_at = now + timedelta(minutes=5)
        if policy.music_track_id is not None:
            session.music_action = ActionProposal(
                action_id=f"music-{uuid4().hex}",
                session_id=session.session_id,
                action_type=ActionType.PLAY_MUSIC,
                payload=MusicActionPayload(track_id=policy.music_track_id),
                proposed_at=now,
                expires_at=expires_at,
            )
        if policy.ac_payload is not None:
            session.ac_action = ActionProposal(
                action_id=f"ac-{uuid4().hex}",
                session_id=session.session_id,
                action_type=ActionType.SET_AC,
                payload=policy.ac_payload,
                proposed_at=now,
                expires_at=expires_at,
            )
        proposals = [
            item for item in (session.music_action, session.ac_action) if item is not None
        ]
        if not proposals:
            return self._transition(
                session,
                DemoState.COMPLETED,
                {"reason": "REACTION_COMPLETE_WITHOUT_ACTION"},
            )

        for proposal in proposals:
            session.authorizations[proposal.action_id] = ActionAuthorization(
                action_id=proposal.action_id,
                action_type=proposal.action_type,
                authorization_status=AuthorizationStatus.PENDING,
                decided_at=None,
                expires_at=proposal.expires_at,
            )
        if self.persistence is not None:
            self.persistence.create_actions(proposals)
            for proposal in proposals:
                self.audit_log.record(
                    session_id=session.session_id,
                    event_type="ACTION_PERSISTED",
                    payload={
                        "authorization_status": AuthorizationStatus.PENDING.value,
                        "execution_status": ExecutionStatus.NOT_STARTED.value,
                        "created_from_model_suggestion": False,
                    },
                    status="CREATED",
                    action_id=proposal.action_id,
                    source_agent="policy-engine",
                )
        self._transition(
            session,
            DemoState.ACTION_PROPOSED,
            {
                "music_action_id": (
                    session.music_action.action_id if session.music_action else None
                ),
                "ac_action_id": session.ac_action.action_id if session.ac_action else None,
                "independent_authorization": True,
            },
        )
        if session.music_action is not None:
            return self._transition(
                session,
                DemoState.WAITING_MUSIC_AUTHORIZATION,
                {"pending_action_id": session.music_action.action_id},
                action_id=session.music_action.action_id,
            )
        return self._transition(
            session,
            DemoState.WAITING_AC_AUTHORIZATION,
            {"pending_action_id": session.ac_action.action_id},
            action_id=session.ac_action.action_id,
        )

    def _prepare_actions(self, session: SessionState) -> SessionState:
        if self.persistence is None:
            memory: dict[str, Any] = self.memory.retrieve_confirmed_preferences()
        else:
            memories = self.list_memories()
            memory = {
                "confirmed": bool(memories),
                "preferences": [item.preference for item in memories],
                "storage": "SQLITE_DEMO",
            }
        self._transition(session, DemoState.MEMORY_RETRIEVED, memory)
        now = self._now()
        expires_at = now + timedelta(minutes=5)
        legacy_label = session.clarification_answer or StateLabel.OTHER
        logical_track_id = logical_track_for_playlist(
            playlist_for_legacy_state(legacy_label)
        )
        session.music_action = ActionProposal(
            action_id=f"music-{uuid4().hex}",
            session_id=session.session_id,
            action_type=ActionType.PLAY_MUSIC,
            payload=MusicActionPayload(track_id=logical_track_id),
            proposed_at=now,
            expires_at=expires_at,
        )
        session.ac_action = ActionProposal(
            action_id=f"ac-{uuid4().hex}",
            session_id=session.session_id,
            action_type=ActionType.SET_AC,
            payload=ACActionPayload(
                device_id="living_room_ac_mock",
                mode=ACMode.HEAT,
                target_temperature=24,
                duration_minutes=30,
            ),
            proposed_at=now,
            expires_at=expires_at,
        )
        for proposal in (session.music_action, session.ac_action):
            session.authorizations[proposal.action_id] = ActionAuthorization(
                action_id=proposal.action_id,
                action_type=proposal.action_type,
                authorization_status=AuthorizationStatus.PENDING,
                decided_at=None,
                expires_at=proposal.expires_at,
            )
        if self.persistence is not None:
            self.persistence.create_actions((session.music_action, session.ac_action))
            for proposal in (session.music_action, session.ac_action):
                self.audit_log.record(
                    session_id=session.session_id,
                    event_type="ACTION_PERSISTED",
                    payload={
                        "authorization_status": AuthorizationStatus.PENDING.value,
                        "execution_status": ExecutionStatus.NOT_STARTED.value,
                    },
                    status="CREATED",
                    action_id=proposal.action_id,
                )
        self._transition(
            session,
            DemoState.ACTION_PROPOSED,
            {
                "music_action_id": session.music_action.action_id,
                "ac_action_id": session.ac_action.action_id,
                "independent_authorization": True,
            },
        )
        return self._transition(
            session,
            DemoState.WAITING_MUSIC_AUTHORIZATION,
            {"pending_action_id": session.music_action.action_id},
            action_id=session.music_action.action_id,
        )

    def _apply_policy(self, session: SessionState) -> SessionState:
        if session.step3_output is None:
            raise InvalidOperation("Step3 output is required before policy evaluation")
        decision = evaluate_clarification(session.step3_output.state_hypotheses)
        self.audit_log.record(
            session_id=session.session_id,
            event_type="POLICY_CLARIFICATION_DECISION",
            payload={
                "required": decision.required,
                "top_confidence": decision.top_confidence,
                "confidence_gap": decision.confidence_gap,
                "reason_codes": list(decision.reason_codes),
                "model_prose_used_as_command": False,
                "model_source": session.model_source,
            },
            status="SUCCEEDED",
            source_agent="policy-engine",
        )
        if decision.required:
            return self._transition(
                session,
                DemoState.CLARIFICATION_REQUIRED,
                {"reason_codes": list(decision.reason_codes)},
            )
        return self._prepare_actions(session)

    def _expected_proposal(self, session: SessionState) -> ActionProposal | None:
        if session.state is DemoState.WAITING_MUSIC_AUTHORIZATION:
            return session.music_action
        if session.state is DemoState.WAITING_AC_AUTHORIZATION:
            return session.ac_action
        return None

    def _continue_after_terminal_action(
        self, session: SessionState, action_type: ActionType
    ) -> SessionState:
        if action_type is ActionType.PLAY_MUSIC and session.ac_action is not None:
            return self._transition(
                session,
                DemoState.WAITING_AC_AUTHORIZATION,
                {"pending_action_id": session.ac_action.action_id},
                action_id=session.ac_action.action_id,
            )
        return self._transition(session, DemoState.COMPLETED, {"mock": True})

    def _save_summary_once(self, session: SessionState) -> None:
        if session.summary_saved or self.persistence is None:
            return
        reaction = session.final_reaction or session.provisional_reaction
        if reaction is None or session.text_analysis is None:
            return
        final_emotion = session.confirmed_emotion
        if final_emotion is None:
            final_emotion = max(
                enumerate(session.text_analysis.state_hypotheses),
                key=lambda item: (item[1].confidence, -item[0]),
            )[1].label
        confidence = next(
            (
                item.confidence
                for item in session.text_analysis.state_hypotheses
                if item.label is final_emotion
            ),
            0.0,
        )
        band = (
            ConfidenceBand.HIGH
            if confidence >= 0.75
            else ConfidenceBand.MEDIUM
            if confidence >= 0.45
            else ConfidenceBand.LOW
        )
        summary = EmotionSummary(
            summary_id=f"summary-{uuid4().hex}",
            session_id=session.session_id,
            final_emotion=final_emotion,
            user_confirmed=session.confirmed_emotion is not None,
            confidence_band=band,
            reaction_tone=reaction.tone,
            music_outcome=self._action_outcome(
                session, session.music_action, "EMOTION_MATCHED_MUSIC"
            ),
            ac_outcome=self._action_outcome(session, session.ac_action, "AC"),
            created_at=self._now(),
        )
        self.persistence.save_emotion_summary(summary)
        session.summary_saved = True
        self.audit_log.record(
            session_id=session.session_id,
            event_type="EMOTION_SUMMARY_SAVED",
            payload={
                "summary_id": summary.summary_id,
                "raw_text_stored": False,
                "reply_text_stored": False,
                "evidence_stored": False,
                "model_payload_stored": False,
                "audio_stored": False,
            },
            status="SUCCEEDED",
            source_agent="summary-store",
        )

    @staticmethod
    def _action_outcome(
        session: SessionState,
        proposal: ActionProposal | None,
        suggestion_type: str,
    ) -> ActionOutcome:
        if proposal is None:
            rejected = any(
                item.suggestion_type == suggestion_type and not item.accepted
                for item in session.policy_suggestion_decisions
            )
            return (
                ActionOutcome.POLICY_REJECTED
                if rejected
                else ActionOutcome.NOT_SUGGESTED
            )
        result = session.results.get(proposal.action_id)
        if result is not None:
            return (
                ActionOutcome.SUCCEEDED
                if result.execution_status is ExecutionStatus.SUCCEEDED
                else ActionOutcome.FAILED
            )
        authorization = session.authorizations.get(proposal.action_id)
        if authorization is not None:
            if authorization.authorization_status is AuthorizationStatus.REJECTED:
                return ActionOutcome.USER_REJECTED
            if authorization.authorization_status is AuthorizationStatus.EXPIRED:
                return ActionOutcome.EXPIRED
        return ActionOutcome.CLOSED_UNDECIDED

    def _record_mock_result(
        self,
        session: SessionState,
        result: ActionResult,
        network_scope: NetworkScope,
    ) -> None:
        self.audit_log.record(
            session_id=session.session_id,
            event_type="ACTION_MOCK_RESULT",
            payload=result.result,
            status=result.execution_status.value,
            action_id=result.action_id,
            network_scope=network_scope,
            source_agent=(
                "music-mock" if result.action_type is ActionType.PLAY_MUSIC else "ac-mock"
            ),
            latency_ms=0,
        )

    def _record_action_result(
        self,
        session: SessionState,
        result: ActionResult,
        network_scope: NetworkScope,
        source_agent: str,
    ) -> None:
        self.audit_log.record(
            session_id=session.session_id,
            event_type="ACTION_RESULT",
            payload=result.result,
            status=result.execution_status.value,
            action_id=result.action_id,
            network_scope=network_scope,
            source_agent=source_agent,
            latency_ms=0,
        )

    def _persist_authorization(
        self,
        session: SessionState,
        action_id: str,
        status: AuthorizationStatus,
        now: datetime,
    ) -> None:
        if self.persistence is None:
            return
        self.persistence.update_authorization(action_id, status, now)
        self.audit_log.record(
            session_id=session.session_id,
            event_type="ACTION_AUTHORIZATION_UPDATED",
            payload={"authorization_status": status.value},
            status=status.value,
            action_id=action_id,
            source_agent="authorization-manager",
        )

    def _persist_running(self, session: SessionState, action_id: str) -> None:
        if self.persistence is None:
            return
        self.persistence.mark_running(action_id)
        self.audit_log.record(
            session_id=session.session_id,
            event_type="ACTION_EXECUTION_UPDATED",
            payload={"execution_status": ExecutionStatus.RUNNING.value},
            status=ExecutionStatus.RUNNING.value,
            action_id=action_id,
            source_agent="action-executor",
        )

    def _persist_result(self, session: SessionState, result: ActionResult) -> None:
        if self.persistence is None:
            return
        self.persistence.finish_action(result)
        self.audit_log.record(
            session_id=session.session_id,
            event_type="ACTION_EXECUTION_UPDATED",
            payload={
                "execution_status": result.execution_status.value,
                "mock": result.result.get("mock", False),
                "physical_action_performed": result.result.get(
                    "physical_action_performed", False
                ),
            },
            status=result.execution_status.value,
            action_id=result.action_id,
            source_agent="action-executor",
        )

    @staticmethod
    def _failed_mock_result(proposal: ActionProposal, now: datetime) -> ActionResult:
        return Orchestrator._failed_action_result(
            proposal, now, mock=True, code="MOCK_EXECUTION_FAILED"
        )

    @staticmethod
    def _failed_action_result(
        proposal: ActionProposal,
        now: datetime,
        *,
        mock: bool,
        code: str,
    ) -> ActionResult:
        return ActionResult(
            action_id=proposal.action_id,
            action_type=proposal.action_type,
            execution_status=ExecutionStatus.FAILED,
            result={
                "code": code,
                "mock": mock,
                "physical_action_performed": False,
            },
            completed_at=now,
        )

    @staticmethod
    def _failed_live_music_result(
        proposal: ActionProposal,
        now: datetime,
        *,
        fallback_reason: str,
        fetch_invoked: bool,
    ) -> ActionResult:
        return ActionResult(
            action_id=proposal.action_id,
            action_type=proposal.action_type,
            execution_status=ExecutionStatus.FAILED,
            result={
                "code": "LOCAL_PLAYBACK_FAILED",
                "mock": False,
                "physical_action_performed": False,
                "playback_started": False,
                "track_id": proposal.payload.track_id,
                "source": "LOCAL_FALLBACK",
                "provider": "LOCAL",
                "fetch_scope": "INTERNET" if fetch_invoked else "NOT_INVOKED",
                "playback_scope": "LOCAL",
                "fallback_used": True,
                "fallback_reason": fallback_reason,
                "preview": False,
            },
            completed_at=now,
        )

    def _require_persistence(self) -> SQLitePersistence:
        if self.persistence is None:
            raise InvalidOperation("SQLite persistence is not enabled")
        self.initialize_persistence()
        return self.persistence

    @staticmethod
    def _ensure_session_active(session: SessionState) -> None:
        if not session.active:
            raise InvalidOperation("session has been reset and is inactive")

    def _transition(
        self,
        session: SessionState,
        target: DemoState,
        details: dict[str, Any],
        *,
        action_id: str | None = None,
    ) -> SessionState:
        previous = session.state
        ensure_state_transition(previous, target)
        session.state = target
        session.state_history.append(target)
        self.audit_log.record(
            session_id=session.session_id,
            event_type="STATE_TRANSITION",
            payload={
                "from_state": previous.value,
                "to_state": target.value,
                "details": details,
            },
            status=target.value,
            action_id=action_id,
        )
        if target is DemoState.COMPLETED and session.runtime_mode == "TEXT_ANALYSIS":
            self._save_summary_once(session)
        return session

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("orchestrator clock must return a timezone-aware datetime")
        return value
