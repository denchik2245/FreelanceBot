from pathlib import Path
from datetime import datetime, timezone

from freelance_bot.models import Project
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
    assert statistics["FL.ru"]["day"] == 1
    assert statistics["FL.ru"]["week"] == 1
    assert statistics["FL.ru"]["month"] == 1
    assert statistics["Profi.ru"]["day"] == 0
    reset_at = store.reset_statistics()
    assert reset_at.tzinfo == timezone.utc
    assert store.statistics_started_at() == reset_at
    assert store.project_statistics()["FL.ru"]["day"] == 0

    project = Project(
        "Kwork",
        "42",
        "Лендинг",
        "",
        "до 500 ₽",
        "https://kwork.ru/projects/42/view",
        "Веб-дизайн",
        datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    store.remember_project(project)
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
