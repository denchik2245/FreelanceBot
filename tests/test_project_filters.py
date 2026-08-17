from datetime import datetime, timezone
from pathlib import Path

import pytest

from freelance_bot.models import Project
from freelance_bot.project_filters import ProjectFilterManager, project_budget
from freelance_bot.storage import ProjectStore


@pytest.fixture
def manager(tmp_path: Path):
    store = ProjectStore(tmp_path / "filters.sqlite3")
    yield ProjectFilterManager(store, default_ai_score=70)
    store.close()


def _project(*, text: str = "Дизайн лендинга в Figma", price: str = "до 50 000 ₽") -> Project:
    return Project("Kwork", "1", text, "Описание", price, "https://example.com", "Дизайн")


def test_filter_settings_are_persisted(manager: ProjectFilterManager) -> None:
    manager.set_min_ai_score(82)
    manager.set_min_budget(25_000)
    manager.set_keywords("include", "Figma, Tilda, figma")
    manager.set_keywords("exclude", "WordPress; SEO")
    manager.set_quiet_hours(23, 8)

    settings = manager.settings()
    assert settings.min_ai_score == 82
    assert settings.min_budget == 25_000
    assert settings.include_keywords == ("figma", "tilda")
    assert settings.exclude_keywords == ("wordpress", "seo")
    assert (settings.quiet_start, settings.quiet_end) == (23, 8)


def test_project_filter_applies_keywords_and_budget(manager: ProjectFilterManager) -> None:
    manager.set_min_budget(60_000)
    assert manager.rejection_reason(_project()) == "бюджет 50000 ₽ ниже минимума"
    assert manager.rejection_reason(_project(price="по договоренности")) is None

    manager.set_min_budget(0)
    manager.set_keywords("include", "tilda")
    assert manager.rejection_reason(_project()) == "нет желательных слов"
    assert manager.rejection_reason(_project(text="Сайт на Tilda")) is None

    manager.set_keywords("exclude", "парсинг")
    assert manager.rejection_reason(_project(text="Дизайн и парсинг сайта")) == "исключающее слово"


def test_quiet_hours_support_overnight_period(manager: ProjectFilterManager) -> None:
    manager.set_quiet_hours(23, 8)

    assert manager.is_quiet_now(datetime(2026, 8, 17, 23, 30, tzinfo=timezone.utc))
    assert manager.is_quiet_now(datetime(2026, 8, 17, 7, 59, tzinfo=timezone.utc))
    assert not manager.is_quiet_now(datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc))


def test_budget_parser_uses_largest_amount() -> None:
    assert project_budget(_project(price="от 10 000 до 25 000 ₽")) == 25_000
    assert project_budget(_project(price="по договоренности")) is None
