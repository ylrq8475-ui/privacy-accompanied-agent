"""Live event audit with optional Phase 1C digest-only persistence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from threading import RLock
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import JsonValue

from backend.app.schemas.events import Event, NetworkScope, PrivacyLevel

if TYPE_CHECKING:
    from backend.app.persistence import SQLitePersistence


EventListener = Callable[[Event], None]


class InMemoryAuditLog:
    """Session-scoped audit storage used by the Phase 1B compatibility path."""

    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._listeners: dict[str, list[EventListener]] = {}
        self._lock = RLock()

    def record(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, JsonValue],
        status: str,
        action_id: str | None = None,
        network_scope: NetworkScope = NetworkScope.LOCAL,
        privacy_level: PrivacyLevel = PrivacyLevel.INTERNAL,
        source_agent: str = "orchestrator",
        confidence: float | None = None,
        latency_ms: int | None = None,
        timestamp: datetime | None = None,
    ) -> Event:
        event = Event(
            event_id=f"event-{uuid4().hex}",
            session_id=session_id,
            timestamp=timestamp or datetime.now(timezone.utc),
            source_agent=source_agent,
            event_type=event_type,
            payload=payload,
            confidence=confidence,
            privacy_level=privacy_level,
            network_scope=network_scope,
            action_id=action_id,
            latency_ms=latency_ms,
            status=status,
        )
        with self._lock:
            self._events.setdefault(session_id, []).append(event)
            listeners = tuple(self._listeners.get(session_id, ()))
        for listener in listeners:
            listener(event)
        return event

    def list_events(self, session_id: str) -> list[Event]:
        with self._lock:
            return list(self._events.get(session_id, ()))

    def subscribe(self, session_id: str, listener: EventListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.setdefault(session_id, []).append(listener)

        def unsubscribe() -> None:
            with self._lock:
                listeners = self._listeners.get(session_id, [])
                if listener in listeners:
                    listeners.remove(listener)

        return unsubscribe


class PersistentAuditLog(InMemoryAuditLog):
    """Keeps live events in memory and stores only metadata plus a payload digest."""

    def __init__(self, persistence: "SQLitePersistence") -> None:
        super().__init__()
        self.persistence = persistence

    def record(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, JsonValue],
        status: str,
        action_id: str | None = None,
        network_scope: NetworkScope = NetworkScope.LOCAL,
        privacy_level: PrivacyLevel = PrivacyLevel.INTERNAL,
        source_agent: str = "orchestrator",
        confidence: float | None = None,
        latency_ms: int | None = None,
        timestamp: datetime | None = None,
    ) -> Event:
        event = super().record(
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            status=status,
            action_id=action_id,
            network_scope=network_scope,
            privacy_level=privacy_level,
            source_agent=source_agent,
            confidence=confidence,
            latency_ms=latency_ms,
            timestamp=timestamp,
        )
        raw_privacy_result = payload.get("privacy_result", "NOT_APPLICABLE")
        privacy_result = (
            raw_privacy_result
            if isinstance(raw_privacy_result, str)
            else "NOT_APPLICABLE"
        )
        self.persistence.append_audit(
            audit_id=event.event_id,
            session_id=event.session_id,
            event_type=event.event_type,
            network_scope=event.network_scope,
            privacy_result=privacy_result,
            payload=event.payload,
            created_at=event.timestamp,
        )
        return event
