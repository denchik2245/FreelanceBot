import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from freelance_bot.main import _format_statistics
from freelance_bot.models import AiAssessment, Project
from freelance_bot.storage import ProjectStore


def test_store(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    assert not store.is_seen("fl:1")
    assert not store.is_initialized()
    assert not store.is_source_initialized("FL.ru")
    store.mark_seen("fl:1", "FL.ru")
    store.mark_initialized()
    store.mark_source_initialized("FL.ru")
    assert store.is_seen("fl:1")
    assert store.is_initialized()
    assert store.is_source_initialized("FL.ru")
    assert store.notifications_enabled("Kwork")
    assert not store.toggle_notifications("Kwork")
    assert not store.notifications_enabled("Kwork")
    statistics = store.project_statistics()
    assert statistics["FL.ru"]["day"] == 0
    assert statistics["FL.ru"]["week"] == 0
    assert statistics["FL.ru"]["month"] == 0
    assert statistics["Profi.ru"]["day"] == 0
    reset_at = store.reset_statistics()
    assert reset_at.tzinfo == timezone.utc
    assert store.statistics_started_at() == reset_at
    assert store.project_statistics()["FL.ru"]["day"] == 0

    project = Project(
        "Kwork",
        "42",
        "Лендинг",
        "Нужно разработать дизайн лендинга",
        "до 500 ₽",
        "https://kwork.ru/projects/42/view",
        "Веб-дизайн",
        datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    store.remember_project(project)
    assessment = AiAssessment(
        project_key=project.key,
        suitable=True,
        score=88,
        reason="Нужен дизайн лендинга",
        response_text="Готов обсудить задачу.",
        filter_model="GigaChat-2",
        response_model="GigaChat-2-Max",
        summary="Клиенту нужен дизайн лендинга.",
    )
    store.remember_ai_assessment(assessment)
    store.remember_ai_response(project.key, "Новый отклик", "GigaChat-2-Max")
    assert store.get_ai_assessment(project.key) == assessment
    assert store.get_ai_response(project.key) == "Новый отклик"
    statistics = store.project_statistics()
    assert statistics["Kwork"]["day"] == 1
    assert statistics["FL.ru"]["day"] == 0
    rejected_project = Project(
        "FL.ru",
        "43",
        "Логотип",
        "Нарисовать логотип",
        "",
        "https://example.com/43",
        "Дизайн",
    )
    store.remember_project(rejected_project)
    store.remember_ai_assessment(
        AiAssessment(
            project_key=rejected_project.key,
            suitable=False,
            score=10,
            reason="Не относится к веб-дизайну",
            response_text="",
            filter_model="GigaChat-2",
            response_model="",
        )
    )
    assert store.ai_rejected_statistics() == {"day": 0, "week": 0, "month": 0}
    store.mark_ai_rejected(rejected_project.key)
    assert store.ai_rejected_statistics() == {"day": 1, "week": 1, "month": 1}
    assert store.project_statistics()["FL.ru"]["day"] == 0
    formatted_statistics = _format_statistics(store)
    assert "Kwork.ru — 1" in formatted_statistics
    assert "Всего подходящих — 1" in formatted_statistics
    assert "Отклонено AI — 1" in formatted_statistics
    assert store.set_project_decision(project.key, "responded")
    assert store.get_project_feedback(project.key) == ("responded", None)
    assert store.feedback_counts()["responded"] == 1
    assert store.active_responses()[0][0] == project
    assert store.set_project_outcome(project.key, "client_replied")
    assert store.get_project_feedback(project.key) == ("responded", "client_replied")
    assert store.feedback_counts()["client_replied"] == 1
    assert store.active_responses()[0][1] == "client_replied"
    assert store.set_project_outcome(project.key, "client_chose_other")
    assert store.active_responses() == []
    assert store.toggle_project_decision(project.key, "rejected") == "rejected"
    assert store.get_project_feedback(project.key) == ("rejected", None)
    assert store.toggle_project_decision(project.key, "rejected") is None
    assert store.get_project_feedback(project.key) is None
    store.close()


def test_store_migrates_and_saves_project_description(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE project_catalog (
            project_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            price TEXT NOT NULL,
            url TEXT NOT NULL,
            category TEXT NOT NULL,
            published_at TEXT,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()
    connection.close()

    store = ProjectStore(path)
    project = Project(
        "FL.ru",
        "123",
        "Дизайн сайта",
        "Полное описание проекта",
        "",
        "https://example.com/123",
        "Дизайн",
    )
    store.remember_project(project)

    assert store.get_project(project.key) == project
    store.close()


def test_store_migrates_ai_assessment_summary(tmp_path: Path) -> None:
    path = tmp_path / "old-assessments.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE project_ai_assessments (
            project_key TEXT PRIMARY KEY,
            suitable INTEGER NOT NULL CHECK(suitable IN (0, 1)),
            score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
            reason TEXT NOT NULL,
            response_text TEXT NOT NULL,
            filter_model TEXT NOT NULL,
            response_model TEXT NOT NULL,
            analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.commit()
    connection.close()

    store = ProjectStore(path)
    store.close()
    connection = sqlite3.connect(path)
    try:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(project_ai_assessments)").fetchall()
        }
    finally:
        connection.close()

    assert "summary" in columns
