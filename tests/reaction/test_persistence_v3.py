from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from backend.app.persistence import SQLitePersistence
from backend.app.persistence.sqlite import _MIGRATION_1, _MIGRATION_2
from backend.app.schemas.persistence import (
    EmotionSummary,
    UserPreferenceConfirmation,
)
from tests.helpers import NOW


class PersistenceV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "reaction.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v3_migrates_legacy_music_once_and_reinitializes_idempotently(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(_MIGRATION_1)
            connection.executescript(_MIGRATION_2)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(1, NOW.isoformat()), (2, NOW.isoformat())],
            )
            connection.execute(
                """
                INSERT INTO memories(memory_id, context, preference, confirmed, created_at, updated_at)
                VALUES ('memory-legacy', 'workday_evening', 'calm_piano', 1, ?, ?)
                """,
                (NOW.isoformat(), NOW.isoformat()),
            )
            connection.commit()
        finally:
            connection.close()

        first = SQLitePersistence(self.path)
        first.initialize()
        second = SQLitePersistence(self.path)
        second.initialize()
        self.assertEqual(second.migration_versions(), [1, 2, 3, 4, 5])
        preferences = second.get_user_preferences()
        self.assertEqual(preferences.music_preference.value, "EMOTION_MATCHED")
        self.assertTrue(preferences.music_preference.confirmed)
        with closing(sqlite3.connect(self.path)) as check:
            self.assertEqual(
                check.execute(
                    "SELECT COUNT(*) FROM user_preferences WHERE preference_key='music_preference'"
                ).fetchone()[0],
                1,
            )

    def test_preferences_distinguish_runtime_default_confirmation_and_delete(self) -> None:
        store = SQLitePersistence(self.path)
        initial = store.get_user_preferences()
        self.assertEqual(initial.reply_style.value, "GENTLE")
        self.assertFalse(initial.reply_style.confirmed)
        self.assertTrue(initial.reply_style.runtime_default)
        record = store.confirm_user_preference(
            UserPreferenceConfirmation(key="reply_style", value="DIRECT"), NOW
        )
        self.assertEqual(record.value, "DIRECT")
        self.assertTrue(store.get_user_preferences().reply_style.confirmed)
        self.assertTrue(store.delete_user_preference("reply_style"))
        self.assertFalse(store.get_user_preferences().reply_style.confirmed)

    def test_fifty_one_summaries_retain_newest_fifty_and_context_is_aggregate_only(self) -> None:
        store = SQLitePersistence(self.path)
        for index in range(51):
            saved = store.save_emotion_summary(
                EmotionSummary(
                    summary_id=f"summary-{index:03d}",
                    session_id=f"session-{index:03d}",
                    final_emotion="HAPPY" if index % 2 else "CALM",
                    user_confirmed=True,
                    confidence_band="HIGH",
                    reaction_tone="CELEBRATORY",
                    music_outcome="NOT_SUGGESTED",
                    ac_outcome="NOT_SUGGESTED",
                    created_at=NOW + timedelta(seconds=index),
                )
            )
            self.assertTrue(saved)
        summaries = store.list_emotion_summaries()
        self.assertEqual(len(summaries), 50)
        self.assertEqual(summaries[0].summary_id, "summary-050")
        self.assertNotIn("summary-000", {item.summary_id for item in summaries})
        self.assertFalse(store.save_emotion_summary(summaries[0]))
        context = store.emotion_history_context()
        self.assertEqual(sum(context.state_counts.values()), 50)
        self.assertEqual(len(context.recent_states), 5)
        self.assertEqual(store.clear_emotion_summaries(), 50)
        self.assertEqual(store.list_emotion_summaries(), [])


if __name__ == "__main__":
    unittest.main()
