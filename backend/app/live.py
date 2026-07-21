"""Phase 3 edge-triggered live/degraded pipeline coordinator."""

from __future__ import annotations

import os
import time
from threading import RLock
from uuid import uuid4

from backend.app.adapters import (
    AdapterError,
    InteractionSource,
    ModelSource,
    PerceptionSource,
    Phase3Settings,
    Step3Adapter,
    StepAudioAdapter,
    TTSResult,
    VisionAdapter,
    VisionObservation,
    fixed_step3_fallback,
)
from backend.app.demo_media import SyntheticSceneCatalog
from backend.app.orchestrator import InvalidOperation, Orchestrator, SessionState
from backend.app.schemas.events import NetworkScope
from backend.app.state_machine import DemoState


FALLBACK_TRANSCRIPT = "今天有点累"


class LiveCoordinator:
    def __init__(
        self,
        orchestrator: Orchestrator,
        *,
        settings: Phase3Settings | None = None,
        vision: VisionAdapter | None = None,
        audio: StepAudioAdapter | None = None,
        step3: Step3Adapter | None = None,
        scenes: SyntheticSceneCatalog | None = None,
        confirmation_samples: int = 2,
    ) -> None:
        self.orchestrator = orchestrator
        self.settings = settings or Phase3Settings.from_environment()
        self.vision = vision or VisionAdapter(
            self.settings.camera_index, self.settings.video_path
        )
        self.audio = audio or StepAudioAdapter(
            self.settings.stepaudio_url, self.settings.stepaudio_filename
        )
        self.step3 = step3 or Step3Adapter(self.settings.step3_url)
        self.scenes = scenes or SyntheticSceneCatalog()
        self.confirmation_samples = max(1, confirmation_samples)
        self.current_session_id: str | None = None
        self._confirmed_present = False
        self._candidate_present: bool | None = None
        self._candidate_count = 0
        self._lock = RLock()
        self.last_visual_attempt: dict[str, object] | None = None

    def health(self) -> dict[str, object]:
        return {
            "mode": "LIVE_WITH_SAFE_FALLBACKS",
            "deployment": {
                "backend": os.environ.get(
                    "SPARK_DEPLOYMENT_TARGET", "LOCAL_WORKSTATION"
                ),
                "console_access": os.environ.get(
                    "SPARK_CONSOLE_ACCESS", "LOCAL_DIRECT"
                ),
            },
            "components": [
                self.vision.health().as_dict(),
                self.audio.health().as_dict(),
                self.step3.health().as_dict(),
                *self.orchestrator.phase4_health(),
            ],
            "raw_media_persisted": False,
            "synthetic_scene_bundled": True,
            "last_visual_attempt": self.last_visual_attempt,
        }

    def analyze_synthetic_scene(
        self, scene_id: str, city_code: str = "310000"
    ) -> tuple[dict[str, object], SessionState | None]:
        """Run one real Step3-VL call against an allowlisted Demo image."""

        attempt_id = f"perception-{uuid4().hex}"
        started = time.monotonic()
        with self._lock:
            try:
                scene = self.scenes.get(scene_id)
                if scene is None:
                    raise AdapterError("DEMO_SCENE_NOT_FOUND", "Demo scene is not allowlisted")
                image, content_type = self.scenes.read_image(scene_id)
                output, model_latency_ms = self.step3.perceive_image(
                    image, content_type
                )
            except AdapterError as error:
                latency_ms = max(0, round((time.monotonic() - started) * 1000))
                self.last_visual_attempt = {
                    "attempt_id": attempt_id,
                    "scene_id": scene_id,
                    "status": "FAILED",
                    "error": error.code,
                    "latency_ms": latency_ms,
                    "session_unchanged": True,
                }
                raise

            perception_source = (
                PerceptionSource.SYNTHETIC_IMAGE
                if scene.synthetic
                else PerceptionSource.DEMO_IMAGE
            )
            observation: dict[str, object] = {
                "attempt_id": attempt_id,
                "scene_id": scene_id,
                "synthetic": scene.synthetic,
                "perception_source": perception_source.value,
                "model_source": ModelSource.STEP3.value,
                "network_scope": NetworkScope.LOCAL.value,
                "person_present": output.person_present,
                "scene_type": output.scene_type.value,
                "scene_summary": output.scene_summary,
                "confidence": output.confidence,
                "confidence_kind": "MODEL_SELF_REPORTED_UNCALIBRATED",
                "evidence": list(output.evidence),
                "latency_ms": model_latency_ms,
                "raw_request_persisted": False,
                "raw_response_persisted": False,
            }
            self.last_visual_attempt = {
                "attempt_id": attempt_id,
                "scene_id": scene_id,
                "status": "SUCCEEDED",
                "person_present": output.person_present,
                "latency_ms": model_latency_ms,
            }

            session: SessionState | None = None
            if self.current_session_id is not None:
                candidate = self.orchestrator.get_session(self.current_session_id)
                if candidate.active:
                    session = candidate
                else:
                    self.current_session_id = None

            if output.person_present:
                if session is None:
                    session = self.orchestrator.begin_live_session(
                        perception_source=perception_source.value,
                        degraded_reasons=[],
                        visual_perception=output,
                        visual_scene_id=scene_id,
                        wait_for_fixed_text=True,
                        city_code=city_code,
                    )
                    self.current_session_id = session.session_id
                else:
                    session.visual_perception = output
                    session.visual_scene_id = scene_id
                    session.person_present = True
                session.component_health["STEP3_VISION"] = {
                    "available": True,
                    "status": "SUCCEEDED",
                    "latency_ms": model_latency_ms,
                }
                self._record_synthetic_vision_call(session, observation, "PERSON_APPEARED")
            elif session is not None:
                session.visual_perception = output
                session.visual_scene_id = scene_id
                session.component_health["STEP3_VISION"] = {
                    "available": True,
                    "status": "SUCCEEDED",
                    "latency_ms": model_latency_ms,
                }
                self._record_synthetic_vision_call(session, observation, "PERSON_DISAPPEARED")
                self.orchestrator.reset_session(session.session_id)
                self.current_session_id = None

            return observation, session

    def poll(self, city_code: str = "310000") -> tuple[VisionObservation, SessionState | None]:
        with self._lock:
            observation = self.vision.observe()
            if observation.person_present == self._candidate_present:
                self._candidate_count += 1
            else:
                self._candidate_present = observation.person_present
                self._candidate_count = 1

            if (
                self._candidate_count >= self.confirmation_samples
                and observation.person_present != self._confirmed_present
            ):
                self._confirmed_present = observation.person_present
                if observation.person_present:
                    session = self.orchestrator.begin_live_session(
                        perception_source=observation.source.value,
                        degraded_reasons=list(observation.degraded_reasons)
                        + (["VISION_STATIC_FALLBACK"] if observation.degraded else []),
                        city_code=city_code,
                    )
                    self.current_session_id = session.session_id
                    session.component_health["VISION"] = {
                        "available": True,
                        "status": observation.source.value,
                        "latency_ms": observation.latency_ms,
                    }
                    self._record_vision_event(session, observation, "PERSON_APPEARED")
                    self._run_asr(session, observation)
                elif self.current_session_id is not None:
                    session = self.orchestrator.get_session(self.current_session_id)
                    if session.active:
                        self._record_vision_event(session, observation, "PERSON_DISAPPEARED")
                        self.orchestrator.reset_session(session.session_id)
                    self.current_session_id = None

            session = (
                self.orchestrator.get_session(self.current_session_id)
                if self.current_session_id is not None
                else None
            )
            return observation, session

    def submit_fallback_transcript(self, session_id: str, text: str) -> SessionState:
        if text != FALLBACK_TRANSCRIPT:
            raise InvalidOperation("live transcript must be the fixed synthetic Demo phrase")
        session = self.orchestrator.get_session(session_id)
        if session.interaction_source != "TEXT_FALLBACK_PENDING":
            raise InvalidOperation("session is not waiting for text fallback")
        self.orchestrator.audit_log.record(
            session_id=session_id,
            event_type="TEXT_FALLBACK_INPUT",
            payload={"text_code": "FIXED_TIREDNESS_PHRASE", "synthetic": True},
            status="ACCEPTED",
            source_agent="text-fallback",
        )
        return self._run_step3(
            session,
            transcript=text,
            interaction_source=InteractionSource.TEXT_FALLBACK,
            jpeg=None,
        )

    def synthesize_current_response(self, session_id: str) -> TTSResult:
        session = self.orchestrator.get_session(session_id)
        text = self._response_text(session)
        started = time.monotonic()
        try:
            result = self.audio.synthesize(text)
            status = "SUCCEEDED"
            reason = None
        except AdapterError as error:
            latency = max(0, round((time.monotonic() - started) * 1000))
            result = TTSResult("TEXT_ONLY", "TEXT_FALLBACK", None, latency)
            status = "DEGRADED"
            reason = error.code
            if error.code not in session.degraded_reasons:
                session.degraded_reasons.append(error.code)
        session.component_health["TTS"] = {
            "available": result.audio_ref is not None,
            "status": status,
            "latency_ms": result.latency_ms,
        }
        self.orchestrator.audit_log.record(
            session_id=session_id,
            event_type="STEPAUDIO_TTS_CALL",
            payload={
                "response_source": result.source,
                "audio_available": result.audio_ref is not None,
                "degraded_reason": reason,
                "raw_audio_persisted": False,
            },
            status=status,
            source_agent="stepaudio-adapter",
            network_scope=NetworkScope.LOCAL,
            latency_ms=result.latency_ms,
        )
        return result

    def reset(self, session_id: str) -> SessionState:
        with self._lock:
            session = self.orchestrator.reset_session(session_id)
            if self.current_session_id == session_id:
                self.current_session_id = None
                self._confirmed_present = False
                self._candidate_present = None
                self._candidate_count = 0
            return session

    def close(self) -> None:
        self.vision.close()
        self.orchestrator.close()

    def _run_asr(self, session: SessionState, observation: VisionObservation) -> None:
        started = time.monotonic()
        try:
            transcript = self.audio.transcribe()
        except AdapterError as error:
            latency = max(0, round((time.monotonic() - started) * 1000))
            self.orchestrator.audit_log.record(
                session_id=session.session_id,
                event_type="STEPAUDIO_ASR_CALL",
                payload={
                    "transcript_persisted": False,
                    "degraded_reason": error.code,
                    "text_fallback_available": True,
                },
                status="DEGRADED",
                source_agent="stepaudio-adapter",
                network_scope=NetworkScope.LOCAL,
                latency_ms=latency,
            )
            self.orchestrator.mark_live_asr_fallback(session.session_id, error.code)
            session.component_health["ASR"] = {
                "available": False,
                "status": error.code,
                "latency_ms": latency,
            }
            return
        session.component_health["ASR"] = {
            "available": True,
            "status": "SUCCEEDED",
            "latency_ms": transcript.latency_ms,
        }
        self.orchestrator.audit_log.record(
            session_id=session.session_id,
            event_type="STEPAUDIO_ASR_CALL",
            payload={"transcript_persisted": False, "text_fallback_available": False},
            status="SUCCEEDED",
            source_agent="stepaudio-adapter",
            network_scope=NetworkScope.LOCAL,
            latency_ms=transcript.latency_ms,
        )
        self._run_step3(
            session,
            transcript=transcript.text,
            interaction_source=InteractionSource.STEPAUDIO_ASR,
            jpeg=observation.jpeg,
        )

    def _run_step3(
        self,
        session: SessionState,
        *,
        transcript: str,
        interaction_source: InteractionSource,
        jpeg: bytes | None,
    ) -> SessionState:
        started = time.monotonic()
        degraded_reason: str | None = None
        try:
            output, latency = self.step3.analyze(transcript, jpeg)
            model_source = ModelSource.STEP3
            status = "SUCCEEDED"
        except AdapterError as error:
            latency = max(0, round((time.monotonic() - started) * 1000))
            output = fixed_step3_fallback()
            model_source = ModelSource.RULE_FALLBACK
            status = "DEGRADED"
            degraded_reason = error.code
        self.orchestrator.audit_log.record(
            session_id=session.session_id,
            event_type="STEP3_MODEL_CALL",
            payload={
                "model_source": model_source.value,
                "degraded_reason": degraded_reason,
                "raw_request_persisted": False,
                "raw_response_persisted": False,
                "authorization_from_model": False,
            },
            status=status,
            source_agent="step3-adapter",
            network_scope=NetworkScope.LOCAL,
            latency_ms=latency,
        )
        session.component_health["STEP3"] = {
            "available": model_source is ModelSource.STEP3,
            "status": status,
            "latency_ms": latency,
        }
        return self.orchestrator.continue_live_pipeline(
            session.session_id,
            transcript=transcript,
            interaction_source=interaction_source.value,
            step3_output=output,
            model_source=model_source.value,
            degraded_reason=degraded_reason,
        )

    def _record_vision_event(
        self, session: SessionState, observation: VisionObservation, status: str
    ) -> None:
        self.orchestrator.audit_log.record(
            session_id=session.session_id,
            event_type="VISION_OBSERVATION",
            payload={
                "edge": status,
                "source": observation.source.value,
                "degraded": observation.degraded,
                "raw_frame_persisted": False,
            },
            status=status,
            source_agent="vision-adapter",
            network_scope=NetworkScope.LOCAL,
            latency_ms=observation.latency_ms,
        )

    def _record_synthetic_vision_call(
        self,
        session: SessionState,
        observation: dict[str, object],
        edge: str,
    ) -> None:
        self.orchestrator.audit_log.record(
            session_id=session.session_id,
            event_type="STEP3_VISION_CALL",
            payload={
                "attempt_id": str(observation["attempt_id"]),
                "scene_id": str(observation["scene_id"]),
                "synthetic": bool(observation["synthetic"]),
                "edge": edge,
                "person_present": bool(observation["person_present"]),
                "scene_type": str(observation["scene_type"]),
                "model_source": ModelSource.STEP3.value,
                "raw_request_persisted": False,
                "raw_response_persisted": False,
                "authorization_from_model": False,
            },
            status="SUCCEEDED",
            source_agent="step3-vision-adapter",
            network_scope=NetworkScope.LOCAL,
            confidence=float(observation["confidence"]),
            latency_ms=int(observation["latency_ms"]),
        )

    @staticmethod
    def _response_text(session: SessionState) -> str:
        if session.step3_output and session.step3_output.clarification_candidates:
            return session.step3_output.clarification_candidates[0].question
        return "系统已完成当前状态分析，请在控制台继续确认。"
