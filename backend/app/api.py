"""Minimal ASGI API for the Mock chain and Phase 1C persistence endpoints."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, ValidationError

from external_connector.weather import RealExternalConnector
from external_connector.audius import AudiusMusicConnector, AudiusSettings
from external_connector.client import RemoteAudiusConnector, RemoteWeatherConnector

from backend.app.console_assets import ConsoleAssetResponse, ConsoleAssets
from backend.app.adapters import AdapterError, Phase3Settings, Step3Adapter, StepAudioAdapter
from backend.app.live import LiveCoordinator
from backend.app.local_music import LocalMusicPlayer
from backend.app.track_catalog import TrackCatalogClient
from backend.app.orchestrator import (
    InvalidOperation,
    MockScenario,
    Orchestrator,
    SessionNotFound,
)
from backend.app.persistence import ActionNotFound, SQLitePersistence
from backend.app.schemas.base import StrictModel
from backend.app.schemas.persistence import (
    MemoryConfirmationRequest,
    StateConfirmationRequest,
    UserPreferenceConfirmation,
)
from backend.app.schemas.analysis import (
    TextAnalysisRequest,
    TextStateModelOutput,
    build_text_analysis_response,
)
from backend.app.schemas.step3 import StateLabel
from backend.app.schemas.events import NetworkScope
from backend.app.schemas.phase4 import WeatherSnapshot
from backend.app.schemas.location import CityCode
from backend.app.schemas.reaction import LLMReaction


ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
MockScenarioValue = Annotated[MockScenario, Field(strict=False)]
StateLabelValue = Annotated[StateLabel, Field(strict=False)]


def _deployment_info() -> dict[str, str]:
    return {
        "backend": os.environ.get("SPARK_DEPLOYMENT_TARGET", "LOCAL_WORKSTATION"),
        "console_access": os.environ.get("SPARK_CONSOLE_ACCESS", "LOCAL_DIRECT"),
    }


def _default_orchestrator() -> Orchestrator:
    deployment_target = os.environ.get(
        "SPARK_DEPLOYMENT_TARGET", "LOCAL_WORKSTATION"
    )
    connector_url = os.environ.get("SPARK_EXTERNAL_CONNECTOR_URL", "").strip()
    if deployment_target == "DGX_SPARK" and not connector_url:
        raise RuntimeError(
            "DGX_SPARK deployment requires isolated SPARK_EXTERNAL_CONNECTOR_URL"
        )
    if connector_url:
        live_connector = RemoteWeatherConnector(connector_url)
        live_audius = RemoteAudiusConnector(connector_url)
    else:
        live_connector = RealExternalConnector()
        live_audius = AudiusMusicConnector(AudiusSettings.from_environment())
    database_path = os.environ.get("SPARK_DATABASE_PATH", "data/demo.sqlite3")
    return Orchestrator(
        persistence=SQLitePersistence(database_path),
        live_connector=live_connector,
        live_music=LocalMusicPlayer(),
        live_audius=live_audius,
        track_catalog=TrackCatalogClient(),
    )


class CreateSessionRequest(StrictModel):
    scenario: MockScenarioValue = MockScenario.SUCCESS
    city_code: CityCode = "310000"


class ClarificationRequest(StrictModel):
    answer: StateLabelValue


class AuthorizationRequest(StrictModel):
    approved: bool
    simulate_failure: bool = False


TextInputValue = Literal["主要是身体累", "主要是心情低落", "其他原因"]


class TextInputRequest(StrictModel):
    text: TextInputValue


class ResetSessionRequest(StrictModel):
    pass


class LiveTranscriptRequest(StrictModel):
    text: Literal["今天有点累"]


class SyntheticSceneRequest(StrictModel):
    scene_id: Literal["indoor_person", "indoor_empty"]
    city_code: CityCode = "310000"


class LivePollRequest(StrictModel):
    city_code: CityCode = "310000"


@dataclass(frozen=True, slots=True)
class AnalysisCacheEntry:
    output: TextStateModelOutput
    reaction: LLMReaction
    weather: WeatherSnapshot


class DemoASGIApp:
    def __init__(
        self,
        orchestrator: Orchestrator | None = None,
        console_assets: ConsoleAssets | None = None,
        live: LiveCoordinator | None = None,
        text_analyzer: Step3Adapter | None = None,
        tts_synthesizer: StepAudioAdapter | None = None,
    ) -> None:
        self.orchestrator = orchestrator or _default_orchestrator()
        self.console_assets = console_assets or ConsoleAssets()
        self.live = live or LiveCoordinator(self.orchestrator)
        self.text_analyzer = text_analyzer or Step3Adapter(
            Phase3Settings.from_environment().step3_url
        )
        self.tts_synthesizer = tts_synthesizer or self.live.audio
        self._analysis_results: OrderedDict[
            str, tuple[float, AnalysisCacheEntry]
        ] = OrderedDict()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        scope_type = scope.get("type")
        if scope_type == "http":
            await self._handle_http(scope, receive, send)
            return
        if scope_type == "websocket":
            await self._handle_websocket(scope, receive, send)
            return
        if scope_type == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        raise RuntimeError(f"unsupported ASGI scope type: {scope_type}")

    async def _handle_http(
        self, scope: dict[str, Any], receive: ASGIReceive, send: ASGISend
    ) -> None:
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))
        try:
            if method == "GET" and path == "/console":
                await _redirect_response(send, "/console/")
                return

            if method == "GET" and path.startswith("/console/"):
                await _asset_response(send, self.console_assets.resolve(path))
                return

            if method == "GET" and path == "/health":
                await _json_response(
                    send,
                    200,
                    {
                        "status": "ok",
                        "mode": "PURE_MOCK",
                        "deployment": _deployment_info(),
                    },
                )
                return

            if method == "GET" and path == "/v1/live/health":
                await _json_response(send, 200, self.live.health())
                return

            if method == "GET" and path == "/v1/live/perception/scenes":
                await _json_response(
                    send,
                    200,
                    {
                        "scenes": [
                            scene.public_dict()
                            for scene in self.live.scenes.list_scenes()
                        ]
                    },
                )
                return

            scene_image_match = re.fullmatch(
                r"/v1/live/perception/scenes/([A-Za-z0-9_-]+)/image", path
            )
            if method == "GET" and scene_image_match:
                scene_id = scene_image_match.group(1)
                if self.live.scenes.get(scene_id) is None:
                    await _json_response(send, 404, {"error": "DEMO_SCENE_NOT_FOUND"})
                    return
                image, content_type = self.live.scenes.read_image(scene_id)
                await _binary_response(send, 200, image, content_type)
                return

            if method == "POST" and path == "/v1/analysis/text":
                request = TextAnalysisRequest.model_validate(await _read_json(receive))
                output, analysis_latency_ms = self.text_analyzer.analyze_text_state(request.text)
                weather = self.orchestrator.load_text_analysis_weather(request.city_code)
                preferences, history = self.orchestrator.reaction_memory_context()
                primary = max(
                    enumerate(output.state_hypotheses),
                    key=lambda item: (item[1].confidence, -item[0]),
                )[1]
                reaction, reaction_latency_ms = self.text_analyzer.generate_reaction(
                    label=primary.label,
                    candidates=output,
                    weather=weather,
                    reply_style=str(preferences.reply_style.value),
                    reply_style_confirmed=preferences.reply_style.confirmed,
                    music_preference=preferences.music_preference.value,
                    music_preference_confirmed=preferences.music_preference.confirmed,
                    history=history,
                    phase="PROVISIONAL",
                    text=request.text,
                )
                entry = AnalysisCacheEntry(output, reaction, weather)
                analysis_id = self._store_analysis_result(entry)
                result = build_text_analysis_response(
                    analysis_id,
                    output,
                    reaction,
                    weather,
                    analysis_latency_ms + reaction_latency_ms,
                )
                await _json_response(send, 200, result.model_dump(mode="json"))
                return

            analysis_session_match = re.fullmatch(
                r"/v1/analysis/text/(analysis-[A-Za-z0-9]+)/sessions", path
            )
            if method == "POST" and analysis_session_match:
                ResetSessionRequest.model_validate(await _read_json(receive))
                entry = self._consume_analysis_result(analysis_session_match.group(1))
                if entry is None:
                    await _json_response(send, 404, {"error": "ANALYSIS_NOT_FOUND"})
                    return
                session = self.orchestrator.start_text_analysis_session(
                    entry.output, entry.reaction, entry.weather
                )
                await _json_response(
                    send, 201, self.orchestrator.snapshot(session.session_id)
                )
                return

            state_confirmation_match = re.fullmatch(
                r"/v1/analysis/sessions/([A-Za-z0-9._:-]+)/state-confirmation",
                path,
            )
            if method == "POST" and state_confirmation_match:
                request = StateConfirmationRequest.model_validate(await _read_json(receive))
                session_id = state_confirmation_match.group(1)
                session = self.orchestrator.get_session(session_id)
                if session.text_analysis is None or session.weather_context is None:
                    raise InvalidOperation("text analysis context is unavailable")
                preferences, history = self.orchestrator.reaction_memory_context()
                reaction, _ = self.text_analyzer.generate_reaction(
                    label=request.label,
                    candidates=session.text_analysis,
                    weather=session.weather_context,
                    reply_style=str(preferences.reply_style.value),
                    reply_style_confirmed=preferences.reply_style.confirmed,
                    music_preference=preferences.music_preference.value,
                    music_preference_confirmed=preferences.music_preference.confirmed,
                    history=history,
                    phase="FINAL",
                    text=None,
                )
                session = self.orchestrator.confirm_text_state(
                    session_id, request.label, reaction
                )
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            if method == "GET" and path == "/v1/user-preferences":
                preferences = self.orchestrator.get_user_preferences()
                await _json_response(send, 200, preferences.model_dump(mode="json"))
                return

            if method == "POST" and path == "/v1/user-preferences/confirm":
                request = UserPreferenceConfirmation.model_validate(await _read_json(receive))
                preference = self.orchestrator.confirm_user_preference(request)
                await _json_response(send, 201, preference.model_dump(mode="json"))
                return

            preference_match = re.fullmatch(
                r"/v1/user-preferences/(reply_style|music_preference)", path
            )
            if method == "DELETE" and preference_match:
                if not self.orchestrator.delete_user_preference(preference_match.group(1)):
                    await _json_response(send, 404, {"error": "PREFERENCE_NOT_FOUND"})
                    return
                await _json_response(send, 200, {"deleted": True})
                return

            if method == "GET" and path == "/v1/emotion-summaries":
                summaries = [
                    item.model_dump(mode="json")
                    for item in self.orchestrator.list_emotion_summaries()
                ]
                await _json_response(send, 200, {"summaries": summaries})
                return

            if method == "DELETE" and path == "/v1/emotion-summaries":
                count = self.orchestrator.clear_emotion_summaries()
                await _json_response(send, 200, {"deleted_count": count})
                return

            if method == "POST" and path == "/v1/live/perception/poll":
                request = LivePollRequest.model_validate(await _read_json(receive))
                observation, session = self.live.poll(request.city_code)
                await _json_response(
                    send,
                    200,
                    {
                        "observation": observation.public_dict(),
                        "session": (
                            self.orchestrator.snapshot(session.session_id)
                            if session is not None
                            else None
                        ),
                    },
                )
                return

            if method == "POST" and path == "/v1/live/perception/analyze":
                request = SyntheticSceneRequest.model_validate(await _read_json(receive))
                try:
                    observation, session = await asyncio.to_thread(
                        self.live.analyze_synthetic_scene,
                        request.scene_id,
                        request.city_code,
                    )
                except AdapterError as error:
                    attempt = self.live.last_visual_attempt or {
                        "attempt_id": f"perception-{uuid4().hex}",
                        "scene_id": request.scene_id,
                        "latency_ms": 0,
                        "session_unchanged": True,
                    }
                    await _json_response(
                        send,
                        503,
                        {
                            "error": error.code,
                            **attempt,
                            "session_unchanged": True,
                        },
                    )
                    return
                await _json_response(
                    send,
                    200,
                    {
                        "attempt_id": observation["attempt_id"],
                        "observation": observation,
                        "session": (
                            self.orchestrator.snapshot(session.session_id)
                            if session is not None
                            else None
                        ),
                    },
                )
                return

            if method == "POST" and path == "/v1/mock/sessions":
                request = CreateSessionRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.start_session(
                    request.scenario, city_code=request.city_code
                )
                await _json_response(send, 201, self.orchestrator.snapshot(session.session_id))
                return

            if method == "POST" and path == "/v1/memories/confirm":
                request = MemoryConfirmationRequest.model_validate(await _read_json(receive))
                memory = self.orchestrator.confirm_memory(request)
                await _json_response(send, 201, memory.model_dump(mode="json"))
                return

            if method == "GET" and path == "/v1/memories":
                memories = [
                    item.model_dump(mode="json")
                    for item in self.orchestrator.list_memories()
                ]
                await _json_response(send, 200, {"memories": memories})
                return

            memory_match = re.fullmatch(r"/v1/memories/([A-Za-z0-9._:-]+)", path)
            if method == "DELETE" and memory_match:
                if not self.orchestrator.delete_memory(memory_match.group(1)):
                    await _json_response(send, 404, {"error": "MEMORY_NOT_FOUND"})
                    return
                await _json_response(send, 200, {"deleted": True})
                return

            action_match = re.fullmatch(r"/v1/actions/([A-Za-z0-9._:-]+)", path)
            if method == "GET" and action_match:
                action = self.orchestrator.get_persisted_action(action_match.group(1))
                await _json_response(send, 200, action.model_dump(mode="json"))
                return

            text_session_match = re.fullmatch(
                r"/v1/analysis/sessions/([A-Za-z0-9._:-]+)", path
            )
            if method == "GET" and text_session_match:
                await _json_response(
                    send, 200, self.orchestrator.snapshot(text_session_match.group(1))
                )
                return

            text_event_match = re.fullmatch(
                r"/v1/analysis/sessions/([A-Za-z0-9._:-]+)/events", path
            )
            if method == "GET" and text_event_match:
                session_id = text_event_match.group(1)
                self.orchestrator.get_session(session_id)
                events = [
                    event.model_dump(mode="json")
                    for event in self.orchestrator.audit_log.list_events(session_id)
                ]
                await _json_response(send, 200, {"events": events})
                return

            text_reset_match = re.fullmatch(
                r"/v1/analysis/sessions/([A-Za-z0-9._:-]+)/reset", path
            )
            if method == "POST" and text_reset_match:
                ResetSessionRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.reset_session(text_reset_match.group(1))
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            text_tts_match = re.fullmatch(
                r"/v1/analysis/sessions/([A-Za-z0-9._:-]+)/tts", path
            )
            if method == "POST" and text_tts_match:
                ResetSessionRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.get_session(text_tts_match.group(1))
                if session.final_reaction is None:
                    raise InvalidOperation("final reaction is required before TTS")
                try:
                    wav, latency_ms = await asyncio.to_thread(
                        self.tts_synthesizer.synthesize_wav,
                        session.final_reaction.reply_text,
                    )
                except AdapterError as error:
                    self.orchestrator.audit_log.record(
                        session_id=session.session_id,
                        event_type="STEPAUDIO_TTS_CALL",
                        payload={
                            "status": "TEXT_ONLY",
                            "reason": error.code,
                            "audio_persisted": False,
                        },
                        status="DEGRADED",
                        source_agent="stepaudio-adapter",
                        network_scope=NetworkScope.LOCAL,
                    )
                    await _json_response(
                        send,
                        200,
                        {"status": "TEXT_ONLY", "error": error.code},
                    )
                    return
                self.orchestrator.audit_log.record(
                    session_id=session.session_id,
                    event_type="STEPAUDIO_TTS_CALL",
                    payload={
                        "status": "READY",
                        "audio_bytes": len(wav),
                        "audio_persisted": False,
                    },
                    status="SUCCEEDED",
                    source_agent="stepaudio-adapter",
                    network_scope=NetworkScope.LOCAL,
                    latency_ms=latency_ms,
                )
                await _binary_response(
                    send, 200, wav, "audio/wav", latency_ms=latency_ms
                )
                return

            session_match = re.fullmatch(r"/v1/mock/sessions/([A-Za-z0-9._:-]+)", path)
            if method == "GET" and session_match:
                await _json_response(
                    send, 200, self.orchestrator.snapshot(session_match.group(1))
                )
                return

            live_session_match = re.fullmatch(
                r"/v1/live/sessions/([A-Za-z0-9._:-]+)", path
            )
            if method == "GET" and live_session_match:
                await _json_response(
                    send, 200, self.orchestrator.snapshot(live_session_match.group(1))
                )
                return

            event_match = re.fullmatch(
                r"/v1/mock/sessions/([A-Za-z0-9._:-]+)/events", path
            )
            if method == "GET" and event_match:
                session_id = event_match.group(1)
                self.orchestrator.get_session(session_id)
                events = [
                    event.model_dump(mode="json")
                    for event in self.orchestrator.audit_log.list_events(session_id)
                ]
                await _json_response(send, 200, {"events": events})
                return

            live_event_match = re.fullmatch(
                r"/v1/live/sessions/([A-Za-z0-9._:-]+)/events", path
            )
            if method == "GET" and live_event_match:
                session_id = live_event_match.group(1)
                self.orchestrator.get_session(session_id)
                events = [
                    event.model_dump(mode="json")
                    for event in self.orchestrator.audit_log.list_events(session_id)
                ]
                await _json_response(send, 200, {"events": events})
                return

            live_transcript_match = re.fullmatch(
                r"/v1/live/sessions/([A-Za-z0-9._:-]+)/transcript", path
            )
            if method == "POST" and live_transcript_match:
                request = LiveTranscriptRequest.model_validate(await _read_json(receive))
                session = self.live.submit_fallback_transcript(
                    live_transcript_match.group(1), request.text
                )
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            live_tts_match = re.fullmatch(
                r"/v1/live/sessions/([A-Za-z0-9._:-]+)/tts", path
            )
            if method == "POST" and live_tts_match:
                ResetSessionRequest.model_validate(await _read_json(receive))
                result = self.live.synthesize_current_response(live_tts_match.group(1))
                await _json_response(send, 200, result.as_dict())
                return

            clarification_match = re.fullmatch(
                r"/v1/mock/sessions/([A-Za-z0-9._:-]+)/clarifications", path
            )
            if method == "POST" and clarification_match:
                request = ClarificationRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.clarify(
                    clarification_match.group(1), request.answer
                )
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            live_clarification_match = re.fullmatch(
                r"/v1/live/sessions/([A-Za-z0-9._:-]+)/clarifications", path
            )
            if method == "POST" and live_clarification_match:
                request = ClarificationRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.clarify(
                    live_clarification_match.group(1), request.answer
                )
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            text_input_match = re.fullmatch(
                r"/v1/mock/sessions/([A-Za-z0-9._:-]+)/text-input", path
            )
            if method == "POST" and text_input_match:
                request = TextInputRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.submit_text(
                    text_input_match.group(1), request.text
                )
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            reset_match = re.fullmatch(
                r"/v1/mock/sessions/([A-Za-z0-9._:-]+)/reset", path
            )
            if method == "POST" and reset_match:
                ResetSessionRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.reset_session(reset_match.group(1))
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            live_reset_match = re.fullmatch(
                r"/v1/live/sessions/([A-Za-z0-9._:-]+)/reset", path
            )
            if method == "POST" and live_reset_match:
                ResetSessionRequest.model_validate(await _read_json(receive))
                session = self.live.reset(live_reset_match.group(1))
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            text_authorization_match = re.fullmatch(
                r"/v1/analysis/sessions/([A-Za-z0-9._:-]+)/actions/"
                r"([A-Za-z0-9._:-]+)/authorization",
                path,
            )
            if method == "POST" and text_authorization_match:
                request = AuthorizationRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.authorize(
                    text_authorization_match.group(1),
                    text_authorization_match.group(2),
                    request.approved,
                    simulate_failure=request.simulate_failure,
                )
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            authorization_match = re.fullmatch(
                r"/v1/mock/sessions/([A-Za-z0-9._:-]+)/actions/"
                r"([A-Za-z0-9._:-]+)/authorization",
                path,
            )
            if method == "POST" and authorization_match:
                request = AuthorizationRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.authorize(
                    authorization_match.group(1),
                    authorization_match.group(2),
                    request.approved,
                    simulate_failure=request.simulate_failure,
                )
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return
            live_authorization_match = re.fullmatch(
                r"/v1/live/sessions/([A-Za-z0-9._:-]+)/actions/"
                r"([A-Za-z0-9._:-]+)/authorization",
                path,
            )
            if method == "POST" and live_authorization_match:
                request = AuthorizationRequest.model_validate(await _read_json(receive))
                session = self.orchestrator.authorize(
                    live_authorization_match.group(1),
                    live_authorization_match.group(2),
                    request.approved,
                    simulate_failure=request.simulate_failure,
                )
                await _json_response(send, 200, self.orchestrator.snapshot(session.session_id))
                return

            await _json_response(send, 404, {"error": "NOT_FOUND"})
        except ValidationError as error:
            await _json_response(
                send,
                422,
                {
                    "error": "VALIDATION_ERROR",
                    "details": error.errors(
                        include_url=False, include_input=False, include_context=False
                    ),
                },
            )
        except json.JSONDecodeError:
            await _json_response(send, 400, {"error": "INVALID_JSON"})
        except SessionNotFound:
            await _json_response(send, 404, {"error": "SESSION_NOT_FOUND"})
        except ActionNotFound:
            await _json_response(send, 404, {"error": "ACTION_NOT_FOUND"})
        except InvalidOperation as error:
            await _json_response(send, 409, {"error": "INVALID_OPERATION", "detail": str(error)})
        except AdapterError as error:
            await _json_response(send, 503, {"error": error.code})

    def _store_analysis_result(self, entry: AnalysisCacheEntry) -> str:
        now = time.monotonic()
        self._discard_expired_analysis_results(now)
        while len(self._analysis_results) >= 32:
            self._analysis_results.popitem(last=False)
        analysis_id = f"analysis-{uuid4().hex}"
        self._analysis_results[analysis_id] = (now + 300.0, entry)
        return analysis_id

    def _consume_analysis_result(
        self, analysis_id: str
    ) -> AnalysisCacheEntry | None:
        now = time.monotonic()
        self._discard_expired_analysis_results(now)
        stored = self._analysis_results.pop(analysis_id, None)
        return stored[1] if stored is not None else None

    def _discard_expired_analysis_results(self, now: float) -> None:
        expired = [
            analysis_id
            for analysis_id, (expires_at, _) in self._analysis_results.items()
            if expires_at <= now
        ]
        for analysis_id in expired:
            self._analysis_results.pop(analysis_id, None)

    async def _handle_websocket(
        self, scope: dict[str, Any], receive: ASGIReceive, send: ASGISend
    ) -> None:
        path = str(scope.get("path", "/"))
        match = re.fullmatch(
            r"/v1/(?:mock|live|analysis)/sessions/([A-Za-z0-9._:-]+)/events", path
        )
        first_message = await receive()
        if first_message.get("type") != "websocket.connect":
            await send({"type": "websocket.close", "code": 4400})
            return
        if not match:
            await send({"type": "websocket.close", "code": 4404})
            return
        session_id = match.group(1)
        try:
            self.orchestrator.get_session(session_id)
        except SessionNotFound:
            await send({"type": "websocket.close", "code": 4404})
            return

        await send({"type": "websocket.accept"})
        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def publish(event: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        unsubscribe = self.orchestrator.audit_log.subscribe(session_id, publish)
        try:
            for event in self.orchestrator.audit_log.list_events(session_id):
                await _websocket_json(send, event.model_dump(mode="json"))

            receive_task = asyncio.create_task(receive())
            while True:
                event_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {receive_task, event_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if receive_task in done:
                    message = receive_task.result()
                    event_task.cancel()
                    if message.get("type") == "websocket.disconnect":
                        return
                    receive_task = asyncio.create_task(receive())
                if event_task in done:
                    event = event_task.result()
                    await _websocket_json(send, event.model_dump(mode="json"))
        finally:
            unsubscribe()

    async def _handle_lifespan(self, receive: ASGIReceive, send: ASGISend) -> None:
        while True:
            message = await receive()
            if message.get("type") == "lifespan.startup":
                self.orchestrator.initialize_persistence()
                await send({"type": "lifespan.startup.complete"})
            elif message.get("type") == "lifespan.shutdown":
                self.live.close()
                await send({"type": "lifespan.shutdown.complete"})
                return


async def _read_json(receive: ASGIReceive) -> dict[str, Any]:
    body = bytearray()
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            continue
        body.extend(message.get("body", b""))
        if not message.get("more_body", False):
            break
    if not body:
        return {}
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise json.JSONDecodeError("request body must be an object", str(value), 0)
    return value


async def _json_response(send: ASGISend, status: int, body: dict[str, Any]) -> None:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(encoded)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": encoded})


async def _binary_response(
    send: ASGISend,
    status: int,
    body: bytes,
    content_type: str,
    *,
    latency_ms: int | None = None,
) -> None:
    headers = [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
    ]
    if latency_ms is not None:
        headers.append((b"x-model-latency-ms", str(latency_ms).encode("ascii")))
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _redirect_response(send: ASGISend, location: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", location.encode("ascii")), (b"content-length", b"0")],
        }
    )
    await send({"type": "http.response.body", "body": b""})


async def _asset_response(send: ASGISend, response: ConsoleAssetResponse) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": response.status,
            "headers": [
                (b"content-type", response.content_type.encode("ascii")),
                (b"content-length", str(len(response.body)).encode("ascii")),
                (b"cache-control", response.cache_control.encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": response.body})


async def _websocket_json(send: ASGISend, body: dict[str, Any]) -> None:
    await send(
        {
            "type": "websocket.send",
            "text": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        }
    )


app = DemoASGIApp()
