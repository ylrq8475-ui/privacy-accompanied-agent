"""Versioned, lazy SQLite storage for confirmed memory and compact action metadata."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from backend.app.schemas.actions import (
    ACActionPayload,
    ActionProposal,
    ActionResult,
    ActionType,
    AuthorizationStatus,
    ExecutionStatus,
    MusicActionPayload,
)
from backend.app.schemas.events import NetworkScope
from backend.app.schemas.persistence import (
    ActionOutcome,
    EmotionHistoryContext,
    EmotionSummary,
    MemoryConfirmationRequest,
    MemoryRecord,
    MusicPreference,
    PersistedAction,
    PersistedAuditRecord,
    ReplyStyle,
    UserPreferenceConfirmation,
    UserPreferenceRecord,
    UserPreferencesResponse,
    UserPreferenceView,
)
from backend.app.schemas.phase4 import WeatherSnapshot, WeatherSource


DEFAULT_DATABASE_PATH = Path("data/demo.sqlite3")
SCHEMA_VERSION = 5


class ActionNotFound(KeyError):
    pass


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    context TEXT NOT NULL CHECK (context = 'workday_evening'),
    preference TEXT NOT NULL CHECK (preference = 'calm_piano'),
    confirmed INTEGER NOT NULL CHECK (confirmed = 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (context, preference)
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('PLAY_MUSIC', 'SET_AC')),
    authorization_status TEXT NOT NULL CHECK (
        authorization_status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'REVOKED')
    ),
    execution_status TEXT NOT NULL CHECK (
        execution_status IN ('NOT_STARTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')
    ),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    authorized_at TEXT,
    executed_at TEXT,
    result TEXT
);

CREATE INDEX IF NOT EXISTS idx_actions_session_id ON actions(session_id);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    network_scope TEXT NOT NULL CHECK (network_scope IN ('LOCAL', 'LAN', 'INTERNET')),
    privacy_result TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_session_id ON audit_log(session_id);
"""

_MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS weather_cache (
    city_code TEXT PRIMARY KEY CHECK (city_code = '310000'),
    temperature_c REAL NOT NULL CHECK (temperature_c >= -80 AND temperature_c <= 80),
    condition TEXT NOT NULL CHECK (length(condition) BETWEEN 1 AND 32),
    provider TEXT NOT NULL CHECK (provider = 'OPEN_METEO'),
    fetched_at TEXT NOT NULL
);
"""

_MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS user_preferences (
    preference_key TEXT PRIMARY KEY CHECK (
        preference_key IN ('reply_style', 'music_preference')
    ),
    preference_value TEXT NOT NULL,
    confirmed INTEGER NOT NULL CHECK (confirmed = 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (preference_key = 'reply_style' AND preference_value IN ('GENTLE', 'CONCISE', 'DIRECT'))
        OR
        (preference_key = 'music_preference' AND preference_value IN ('CALM_PIANO', 'NONE'))
    )
);

INSERT OR IGNORE INTO user_preferences(
    preference_key, preference_value, confirmed, created_at, updated_at
)
SELECT 'music_preference', 'CALM_PIANO', 1, MIN(created_at), MAX(updated_at)
FROM memories
WHERE context = 'workday_evening' AND preference = 'calm_piano' AND confirmed = 1
HAVING COUNT(*) > 0;

CREATE TABLE IF NOT EXISTS emotion_summaries (
    summary_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    final_emotion TEXT NOT NULL CHECK (final_emotion IN (
        'PHYSICAL_FATIGUE', 'EMOTIONAL_LOW', 'HAPPY', 'STRESSED', 'ANXIOUS',
        'LONELY', 'ANGRY', 'CALM', 'OTHER'
    )),
    user_confirmed INTEGER NOT NULL CHECK (user_confirmed IN (0, 1)),
    confidence_band TEXT NOT NULL CHECK (confidence_band IN ('LOW', 'MEDIUM', 'HIGH')),
    reaction_tone TEXT NOT NULL CHECK (reaction_tone IN (
        'SUPPORTIVE', 'CELEBRATORY', 'CALMING', 'GROUNDING', 'NEUTRAL'
    )),
    music_outcome TEXT NOT NULL CHECK (music_outcome IN (
        'NOT_SUGGESTED', 'POLICY_REJECTED', 'USER_REJECTED', 'EXPIRED',
        'SUCCEEDED', 'FAILED', 'CLOSED_UNDECIDED'
    )),
    ac_outcome TEXT NOT NULL CHECK (ac_outcome IN (
        'NOT_SUGGESTED', 'POLICY_REJECTED', 'USER_REJECTED', 'EXPIRED',
        'SUCCEEDED', 'FAILED', 'CLOSED_UNDECIDED'
    )),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_emotion_summaries_created_at
ON emotion_summaries(created_at, summary_id);
"""

_MIGRATION_4 = """
ALTER TABLE user_preferences RENAME TO user_preferences_v3;

CREATE TABLE user_preferences (
    preference_key TEXT PRIMARY KEY CHECK (
        preference_key IN ('reply_style', 'music_preference')
    ),
    preference_value TEXT NOT NULL,
    confirmed INTEGER NOT NULL CHECK (confirmed = 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (preference_key = 'reply_style' AND preference_value IN ('GENTLE', 'CONCISE', 'DIRECT'))
        OR
        (preference_key = 'music_preference' AND preference_value IN ('EMOTION_MATCHED', 'NONE'))
    )
);

INSERT INTO user_preferences(
    preference_key, preference_value, confirmed, created_at, updated_at
)
SELECT
    preference_key,
    CASE
        WHEN preference_key = 'music_preference' AND preference_value = 'CALM_PIANO'
            THEN 'EMOTION_MATCHED'
        ELSE preference_value
    END,
    confirmed,
    created_at,
    updated_at
FROM user_preferences_v3;

DROP TABLE user_preferences_v3;
"""

_MIGRATION_5 = """
ALTER TABLE weather_cache RENAME TO weather_cache_v2;

CREATE TABLE weather_cache (
    city_code TEXT PRIMARY KEY CHECK (
        city_code IN ('110000', '310000', '440100', '440300', '510100')
    ),
    temperature_c REAL NOT NULL CHECK (temperature_c >= -80 AND temperature_c <= 80),
    condition TEXT NOT NULL CHECK (length(condition) BETWEEN 1 AND 32),
    provider TEXT NOT NULL CHECK (provider = 'OPEN_METEO'),
    fetched_at TEXT NOT NULL
);

INSERT INTO weather_cache(city_code, temperature_c, condition, provider, fetched_at)
SELECT city_code, temperature_c, condition, provider, fetched_at
FROM weather_cache_v2;

DROP TABLE weather_cache_v2;
"""

_MIGRATIONS = {
    1: _MIGRATION_1,
    2: _MIGRATION_2,
    3: _MIGRATION_3,
    4: _MIGRATION_4,
    5: _MIGRATION_5,
}


def canonical_payload_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("SQLite timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLitePersistence:
    """Small repository facade; creating an instance performs no filesystem I/O."""

    def __init__(self, path: str | Path = DEFAULT_DATABASE_PATH) -> None:
        self.path = Path(path)
        self._initialized = False
        self._lock = RLock()

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._raw_connection()) as connection:
                try:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS schema_migrations ("
                        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                    )
                    applied = {
                        row[0]
                        for row in connection.execute(
                            "SELECT version FROM schema_migrations"
                        )
                    }
                    for version, script in _MIGRATIONS.items():
                        if version in applied:
                            continue
                        connection.executescript(script)
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            (version, datetime.now(timezone.utc).isoformat()),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            self._initialized = True

    def migration_versions(self) -> list[int]:
        with self._connection() as connection:
            return [row["version"] for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )]

    def confirm_memory(
        self, request: MemoryConfirmationRequest, now: datetime
    ) -> MemoryRecord:
        memory_id = f"memory-{uuid4().hex}"
        timestamp = _timestamp(now)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO memories(
                    memory_id, context, preference, confirmed, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(context, preference) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (memory_id, request.context, request.preference, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM memories WHERE context = ? AND preference = ?",
                (request.context, request.preference),
            ).fetchone()
        return self._memory_from_row(row)

    def list_memories(self) -> list[MemoryRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memories ORDER BY created_at, memory_id"
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def delete_memory(self, memory_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE memory_id = ?", (memory_id,)
            )
        return cursor.rowcount == 1

    def confirm_user_preference(
        self, request: UserPreferenceConfirmation, now: datetime
    ) -> UserPreferenceRecord:
        timestamp = _timestamp(now)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO user_preferences(
                    preference_key, preference_value, confirmed, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(preference_key) DO UPDATE SET
                    preference_value = excluded.preference_value,
                    confirmed = 1,
                    updated_at = excluded.updated_at
                """,
                (request.key, request.value, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM user_preferences WHERE preference_key = ?",
                (request.key,),
            ).fetchone()
        return self._preference_from_row(row)

    def list_user_preference_records(self) -> list[UserPreferenceRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM user_preferences ORDER BY preference_key"
            ).fetchall()
        return [self._preference_from_row(row) for row in rows]

    def get_user_preferences(self) -> UserPreferencesResponse:
        records = {item.key: item for item in self.list_user_preference_records()}
        reply = records.get("reply_style")
        music = records.get("music_preference")
        return UserPreferencesResponse(
            reply_style=UserPreferenceView(
                value=reply.value if reply is not None else ReplyStyle.GENTLE.value,
                confirmed=reply is not None,
                runtime_default=reply is None,
            ),
            music_preference=UserPreferenceView(
                value=music.value if music is not None else None,
                confirmed=music is not None,
                runtime_default=False,
            ),
        )

    def delete_user_preference(self, key: str) -> bool:
        if key not in {"reply_style", "music_preference"}:
            return False
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM user_preferences WHERE preference_key = ?", (key,)
            )
        return cursor.rowcount == 1

    def save_emotion_summary(self, summary: EmotionSummary) -> bool:
        """Insert once per session and retain only the newest 50 in one transaction."""

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO emotion_summaries(
                        summary_id, session_id, final_emotion, user_confirmed,
                        confidence_band, reaction_tone, music_outcome, ac_outcome,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary.summary_id,
                        summary.session_id,
                        summary.final_emotion.value,
                        int(summary.user_confirmed),
                        summary.confidence_band.value,
                        summary.reaction_tone.value,
                        summary.music_outcome.value,
                        summary.ac_outcome.value,
                        _timestamp(summary.created_at),
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM emotion_summaries
                    WHERE summary_id NOT IN (
                        SELECT summary_id FROM emotion_summaries
                        ORDER BY created_at DESC, summary_id DESC
                        LIMIT 50
                    )
                    """
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return cursor.rowcount == 1

    def list_emotion_summaries(self) -> list[EmotionSummary]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM emotion_summaries
                ORDER BY created_at DESC, summary_id DESC
                LIMIT 50
                """
            ).fetchall()
        return [self._summary_from_row(row) for row in rows]

    def clear_emotion_summaries(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM emotion_summaries")
        return cursor.rowcount

    def emotion_history_context(self) -> EmotionHistoryContext:
        summaries = self.list_emotion_summaries()
        counts: dict[str, int] = {}
        for summary in summaries:
            key = summary.final_emotion.value
            counts[key] = counts.get(key, 0) + 1
        return EmotionHistoryContext(
            state_counts=counts,
            recent_states=[item.final_emotion for item in summaries[:5]],
        )

    def create_actions(self, proposals: Iterable[ActionProposal]) -> None:
        values = []
        for proposal in proposals:
            payload = proposal.payload.model_dump(mode="json")
            values.append(
                (
                    proposal.action_id,
                    proposal.session_id,
                    proposal.action_type.value,
                    AuthorizationStatus.PENDING.value,
                    ExecutionStatus.NOT_STARTED.value,
                    _json(payload),
                    _timestamp(proposal.proposed_at),
                    _timestamp(proposal.expires_at),
                )
            )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany(
                    """
                    INSERT INTO actions(
                        action_id, session_id, action_type, authorization_status,
                        execution_status, payload, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def update_authorization(
        self, action_id: str, status: AuthorizationStatus, now: datetime
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE actions
                SET authorization_status = ?, authorized_at = ?
                WHERE action_id = ?
                """,
                (status.value, _timestamp(now), action_id),
            )
        if cursor.rowcount != 1:
            raise ActionNotFound(action_id)

    def mark_running(self, action_id: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE actions SET execution_status = ? WHERE action_id = ?",
                (ExecutionStatus.RUNNING.value, action_id),
            )
        if cursor.rowcount != 1:
            raise ActionNotFound(action_id)

    def finish_action(self, result: ActionResult) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE actions
                SET execution_status = ?, executed_at = ?, result = ?
                WHERE action_id = ?
                """,
                (
                    result.execution_status.value,
                    _timestamp(result.completed_at),
                    _json(result.result),
                    result.action_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ActionNotFound(result.action_id)

    def get_action(self, action_id: str) -> PersistedAction:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise ActionNotFound(action_id)
        return self._action_from_row(row)

    def list_actions(self, session_id: str | None = None) -> list[PersistedAction]:
        with self._connection() as connection:
            if session_id is None:
                rows = connection.execute(
                    "SELECT * FROM actions ORDER BY created_at, action_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM actions WHERE session_id = ? ORDER BY created_at, action_id",
                    (session_id,),
                ).fetchall()
        return [self._action_from_row(row) for row in rows]

    def append_audit(
        self,
        *,
        audit_id: str,
        session_id: str,
        event_type: str,
        network_scope: NetworkScope,
        privacy_result: str,
        payload: dict[str, object],
        created_at: datetime,
    ) -> PersistedAuditRecord:
        record = PersistedAuditRecord(
            audit_id=audit_id,
            session_id=session_id,
            event_type=event_type,
            network_scope=network_scope,
            privacy_result=privacy_result,
            payload_digest=canonical_payload_digest(payload),
            created_at=created_at,
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO audit_log(
                    audit_id, session_id, event_type, network_scope,
                    privacy_result, payload_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.audit_id,
                    record.session_id,
                    record.event_type,
                    record.network_scope.value,
                    record.privacy_result,
                    record.payload_digest,
                    _timestamp(record.created_at),
                ),
            )
        return record

    def list_audits(self, session_id: str | None = None) -> list[PersistedAuditRecord]:
        with self._connection() as connection:
            if session_id is None:
                rows = connection.execute(
                    "SELECT * FROM audit_log ORDER BY created_at, audit_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE session_id = ? ORDER BY created_at, audit_id",
                    (session_id,),
                ).fetchall()
        return [self._audit_from_row(row) for row in rows]

    def save_weather_cache(self, snapshot: WeatherSnapshot) -> None:
        if snapshot.source is not WeatherSource.REAL_API or snapshot.provider != "OPEN_METEO":
            raise ValueError("only normalized real weather can populate the cache")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO weather_cache(
                    city_code, temperature_c, condition, provider, fetched_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(city_code) DO UPDATE SET
                    temperature_c = excluded.temperature_c,
                    condition = excluded.condition,
                    provider = excluded.provider,
                    fetched_at = excluded.fetched_at
                """,
                (
                    snapshot.city_code,
                    snapshot.temperature_c,
                    snapshot.condition,
                    snapshot.provider,
                    _timestamp(snapshot.fetched_at),
                ),
            )

    def get_weather_cache(self, city_code: str) -> WeatherSnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM weather_cache WHERE city_code = ?", (city_code,)
            ).fetchone()
        if row is None:
            return None
        return WeatherSnapshot(
            city_code=row["city_code"],
            temperature_c=row["temperature_c"],
            condition=row["condition"],
            source=WeatherSource.CACHE,
            fetched_at=_parse_timestamp(row["fetched_at"]),
            provider=row["provider"],
        )

    def recover_actions(self, now: datetime) -> tuple[int, int]:
        """Coordinate unsafe restart states without invoking any executor."""

        now_text = _timestamp(now)
        expired = 0
        interrupted = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                expired_rows = connection.execute(
                    """
                    SELECT action_id, session_id FROM actions
                    WHERE authorization_status = 'APPROVED'
                      AND execution_status = 'NOT_STARTED'
                      AND expires_at <= ?
                    """,
                    (now_text,),
                ).fetchall()
                running_rows = connection.execute(
                    """
                    SELECT action_id, session_id FROM actions
                    WHERE execution_status = 'RUNNING'
                    """
                ).fetchall()
                for row in expired_rows:
                    connection.execute(
                        "UPDATE actions SET authorization_status = 'EXPIRED' WHERE action_id = ?",
                        (row["action_id"],),
                    )
                    self._append_recovery_audit(
                        connection,
                        session_id=row["session_id"],
                        event_type="ACTION_RECOVERY_EXPIRED",
                        payload={"action_id": row["action_id"], "auto_executed": False},
                        now=now,
                    )
                    expired += 1
                for row in running_rows:
                    result = {
                        "code": "INTERRUPTED_BY_RESTART",
                        "mock": True,
                        "physical_action_performed": False,
                    }
                    connection.execute(
                        """
                        UPDATE actions
                        SET execution_status = 'FAILED', executed_at = ?, result = ?
                        WHERE action_id = ?
                        """,
                        (now_text, _json(result), row["action_id"]),
                    )
                    self._append_recovery_audit(
                        connection,
                        session_id=row["session_id"],
                        event_type="ACTION_RECOVERY_INTERRUPTED",
                        payload={"action_id": row["action_id"], "auto_retried": False},
                        now=now,
                    )
                    interrupted += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return expired, interrupted

    def _append_recovery_audit(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log(
                audit_id, session_id, event_type, network_scope,
                privacy_result, payload_digest, created_at
            ) VALUES (?, ?, ?, 'LOCAL', 'NOT_APPLICABLE', ?, ?)
            """,
            (
                f"audit-{uuid4().hex}",
                session_id,
                event_type,
                canonical_payload_digest(payload),
                _timestamp(now),
            ),
        )

    @contextmanager
    def _connection(self):
        self.initialize()
        connection = self._raw_connection()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _raw_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"],
            context=row["context"],
            preference=row["preference"],
            confirmed=bool(row["confirmed"]),
            created_at=_parse_timestamp(row["created_at"]),
            updated_at=_parse_timestamp(row["updated_at"]),
        )

    @staticmethod
    def _preference_from_row(row: sqlite3.Row) -> UserPreferenceRecord:
        return UserPreferenceRecord(
            key=row["preference_key"],
            value=row["preference_value"],
            confirmed=bool(row["confirmed"]),
            created_at=_parse_timestamp(row["created_at"]),
            updated_at=_parse_timestamp(row["updated_at"]),
        )

    @staticmethod
    def _summary_from_row(row: sqlite3.Row) -> EmotionSummary:
        return EmotionSummary(
            summary_id=row["summary_id"],
            session_id=row["session_id"],
            final_emotion=row["final_emotion"],
            user_confirmed=bool(row["user_confirmed"]),
            confidence_band=row["confidence_band"],
            reaction_tone=row["reaction_tone"],
            music_outcome=row["music_outcome"],
            ac_outcome=row["ac_outcome"],
            created_at=_parse_timestamp(row["created_at"]),
        )

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> PersistedAction:
        action_type = ActionType(row["action_type"])
        payload_data = json.loads(row["payload"])
        payload = (
            MusicActionPayload.model_validate(payload_data)
            if action_type is ActionType.PLAY_MUSIC
            else ACActionPayload.model_validate(payload_data)
        )
        return PersistedAction(
            action_id=row["action_id"],
            session_id=row["session_id"],
            action_type=action_type,
            authorization_status=AuthorizationStatus(row["authorization_status"]),
            execution_status=ExecutionStatus(row["execution_status"]),
            payload=payload,
            created_at=_parse_timestamp(row["created_at"]),
            expires_at=_parse_timestamp(row["expires_at"]),
            authorized_at=_parse_timestamp(row["authorized_at"]),
            executed_at=_parse_timestamp(row["executed_at"]),
            result=json.loads(row["result"]) if row["result"] is not None else None,
        )

    @staticmethod
    def _audit_from_row(row: sqlite3.Row) -> PersistedAuditRecord:
        return PersistedAuditRecord(
            audit_id=row["audit_id"],
            session_id=row["session_id"],
            event_type=row["event_type"],
            network_scope=NetworkScope(row["network_scope"]),
            privacy_result=row["privacy_result"],
            payload_digest=row["payload_digest"],
            created_at=_parse_timestamp(row["created_at"]),
        )
