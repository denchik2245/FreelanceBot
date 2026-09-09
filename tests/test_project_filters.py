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

    settings = manager.settings()
    assert settings.min_ai_score == 82
    assert settings.min_budget == 25_000


def test_project_filter_applies_budget(manager: ProjectFilterManager) -> None:
    manager.set_min_budget(60_000)
    assert manager.rejection_reason(_project()) == "бюджет 50000 ₽ ниже минимума"
    assert manager.rejection_reason(_project(price="по договоренности")) is None

def test_budget_parser_uses_largest_amount() -> None:
    assert project_budget(_project(price="от 10 000 до 25 000 ₽")) == 25_000
    assert project_budget(_project(price="по договоренности")) is None
