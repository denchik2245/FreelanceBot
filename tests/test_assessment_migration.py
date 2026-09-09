import sqlite3
from pathlib import Path

from freelance_bot.models import AiAssessment
from freelance_bot.storage import ProjectStore


def test_old_assessments_get_empty_revision_and_keep_response(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE project_ai_assessments (
                project_key TEXT PRIMARY KEY, suitable INTEGER, score INTEGER,
                reason TEXT, summary TEXT, response_text TEXT,
                filter_model TEXT, response_model TEXT, analyzed_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO project_ai_assessments VALUES (
                'Kwork:42', 1, 85, 'Old reason', 'Summary', 'Saved response',
                'filter', 'writer', '2026-09-01 00:00:00'
            )
            """
        )
    store = ProjectStore(path)
    try:
        old = store.get_ai_assessment("Kwork:42")
        assert old.filter_revision == ""
        assert old.score == 85
        store.remember_ai_assessment(
            AiAssessment(
                project_key="Kwork:42", suitable=False, score=10,
                reason="New reason", summary="Summary", response_text="",
                filter_model="filter", response_model="", filter_revision="new-rules",
            )
        )
        assert store.get_ai_assessment("Kwork:42").score == 10
        assert store.get_ai_response("Kwork:42") == "Saved response"
    finally:
        store.close()
    # Opening an already migrated database must retain the new cache metadata.
    reopened = ProjectStore(path)
    try:
        assert reopened.get_ai_assessment("Kwork:42").filter_revision == "new-rules"
    finally:
        reopened.close()
