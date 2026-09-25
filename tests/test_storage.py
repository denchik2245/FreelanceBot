import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from freelance_bot.main import _format_statistics
from freelance_bot.models import AiAssessment, Project
from freelance_bot.storage import ProjectStore


def test_store(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    assert not store.is_seen("fl:1")
    assert not store.is_initialized()
    assert not store.is_source_initialized("FL.ru")
    assert not store.is_mail_notification_seen("message-1")
    store.mark_mail_notification_seen("message-1")
    store.mark_mail_notification_seen("message-1")
    assert store.is_mail_notification_seen("message-1")
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


def test_daily_statistics_uses_display_timezone_and_includes_empty_days(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "daily.sqlite3")
    store.set_state("statistics_started_at", "2026-07-01 00:00:00.000000")
    project = Project(
        "Kwork",
        "daily-1",
        "Лендинг",
        "Описание",
        "",
        "https://example.com/daily-1",
        "Дизайн",
    )
    store.remember_project(project)
    store.remember_ai_assessment(
        AiAssessment(
            project_key=project.key,
            suitable=True,
            score=90,
            reason="Подходит",
            response_text="",
            filter_model="GigaChat-2",
            response_model="",
            summary="Нужен лендинг",
        )
    )
    store._connection.execute(
        "UPDATE project_ai_assessments SET analyzed_at = ? WHERE project_key = ?",
        ("2026-08-08 20:30:00.000000", project.key),
    )
    store._connection.commit()

    display_timezone = timezone(timedelta(hours=5))
    daily = store.daily_project_statistics(
        days=3,
        display_timezone=display_timezone,
        now=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
    )

    assert [day.isoformat() for day in daily] == ["2026-08-08", "2026-08-09", "2026-08-10"]
    assert daily[datetime(2026, 8, 9).date()]["Kwork"] == 1
    assert daily[datetime(2026, 8, 8).date()]["Kwork"] == 0
    assert daily[datetime(2026, 8, 10).date()]["rejected"] == 0

    month_to_date = store.daily_project_statistics(
        display_timezone=display_timezone,
        now=datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
    )
    assert len(month_to_date) == 9
    assert next(iter(month_to_date)).isoformat() == "2026-08-01"
    assert next(reversed(month_to_date)).isoformat() == "2026-08-09"
    store.close()


def test_formatted_statistics_labels_weekends() -> None:
    saturday = datetime(2026, 8, 8).date()
    sunday = datetime(2026, 8, 9).date()

    class FakeStore:
        def project_statistics(self) -> dict[str, dict[str, int]]:
            return {
                source: {"day": 0, "week": 0, "month": 0}
                for source in ("Kwork", "FL.ru", "Profi.ru")
            }

        def ai_rejected_statistics(self) -> dict[str, int]:
            return {"day": 0, "week": 0, "month": 0}

        def daily_project_statistics(self, **_: object) -> dict[object, dict[str, int]]:
            return {
                saturday: {"Kwork": 1, "FL.ru": 0, "Profi.ru": 0, "rejected": 2},
                sunday: {"Kwork": 0, "FL.ru": 1, "Profi.ru": 0, "rejected": 3},
            }

        def statistics_started_at(self) -> datetime:
            return datetime(2026, 8, 1, tzinfo=timezone.utc)

    message = _format_statistics(FakeStore())  # type: ignore[arg-type]

    assert "09.08.2026 (воскресенье) — подходящих 1" in message
    assert "08.08.2026 (суббота) — подходящих 1" in message
    assert "По дням с начала месяца" in message
    daily_section = message.split("По дням с начала месяца", maxsplit=1)[1]
    assert "отклонено AI" not in daily_section
    assert "09.08.2026 (воскресенье) — подходящих 1\n\n08.08.2026" in daily_section
    assert "09.08.2026 (воскресенье) — Kwork" not in message


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


def test_kwork_restarted_publication_is_treated_as_new(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "restarted.sqlite3")
    original_date = datetime(2026, 7, 27, 8, 34, 20, tzinfo=UTC)
    original = Project(
        "Kwork",
        "3225088",
        "Дизайн сайта доставки еды из ресторанов",
        "Нужно разработать дизайн сайта",
        "до 25 000 ₽",
        "https://kwork.ru/projects/3225088/view",
        "Веб и мобильный дизайн",
        original_date,
    )
    store.remember_project(original)
    store.mark_seen(original.key, original.source)

    same_publication = Project(
        source=original.source,
        external_id=original.external_id,
        title=original.title,
        description=original.description,
        price=original.price,
        url=original.url,
        category=original.category,
        published_at=original_date,
        publication_id=str(int(original_date.timestamp())),
    )
    assert store.is_project_seen(same_publication)

    restarted_date = original_date + timedelta(days=2)
    restarted = Project(
        source=original.source,
        external_id=original.external_id,
        title=original.title,
        description=original.description,
        price=original.price,
        url=original.url,
        category=original.category,
        published_at=restarted_date,
        publication_id=str(int(restarted_date.timestamp())),
    )
    assert not store.is_project_seen(restarted)

    store.remember_project(restarted)
    store.mark_seen(restarted.key, restarted.source)
    assert store.is_project_seen(restarted)
    assert store.get_project(restarted.key) == restarted
    store.close()


def test_publication_compatibility_uses_first_seen_without_catalog(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "legacy-first-run.sqlite3")
    canonical_key = "Kwork:77"
    store.mark_seen(canonical_key, "Kwork", count_for_statistics=False)
    store._connection.execute(
        "UPDATE seen_projects SET first_seen_at = ? WHERE project_key = ?",
        ("2026-07-27 10:00:00.000", canonical_key),
    )
    store._connection.commit()

    same_old_project = Project(
        "Kwork",
        "77",
        "Старый проект",
        "Описание",
        "",
        "https://kwork.ru/projects/77/view",
        "Дизайн",
        datetime(2026, 7, 27, 9, 55, tzinfo=UTC),
        str(int(datetime(2026, 7, 27, 9, 55, tzinfo=UTC).timestamp())),
    )
    restarted_project = Project(
        "Kwork",
        "77",
        "Перезапущенный проект",
        "Описание",
        "",
        "https://kwork.ru/projects/77/view",
        "Дизайн",
        datetime(2026, 7, 28, 9, 55, tzinfo=UTC),
        str(int(datetime(2026, 7, 28, 9, 55, tzinfo=UTC).timestamp())),
    )

    assert store.is_project_seen(same_old_project)
    assert not store.is_project_seen(restarted_project)
    store.close()
