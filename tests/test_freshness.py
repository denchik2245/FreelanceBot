from datetime import datetime, timedelta, timezone

from freelance_bot.main import _is_fresh
from freelance_bot.models import Project


def _project(published_at: datetime | None) -> Project:
    return Project("Kwork", "1", "Проект", "", "500 ₽", "https://x", "Дизайн", published_at)


def test_only_projects_from_last_24_hours_are_fresh() -> None:
    now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    assert _is_fresh(_project(now - timedelta(hours=23, minutes=59)), now=now)
    assert not _is_fresh(_project(now - timedelta(hours=24, minutes=1)), now=now)
    assert not _is_fresh(_project(None), now=now)
