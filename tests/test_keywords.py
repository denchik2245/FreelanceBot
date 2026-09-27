from freelance_bot.keywords import matches_project_keywords, matching_project_keywords
from freelance_bot.models import Project


def _project(title: str, description: str = "", category: str = "Все категории") -> Project:
    return Project("FL.ru", "1", title, description, "", "https://example.com", category)


def test_matches_inflected_russian_keywords_and_normalized_phrases() -> None:
    project = _project("Создание сайта", "Макеты сделаны в UI/UX и Zero-Block")

    matches = matching_project_keywords(project)

    assert "сайт" in matches
    assert "макет" in matches
    assert "ui" in matches
    assert "ux" in matches
    assert "zero block" in matches


def test_rejects_project_without_relevant_keywords() -> None:
    assert not matches_project_keywords(_project("Перевести договор на английский язык"))


def test_mobile_app_screens_reach_semantic_filter() -> None:
    project = _project(
        "Отрисовать экраны мобильного приложения",
        "Нужны пользовательские сценарии и экраны приложения для записи на занятия",
    )
    assert matches_project_keywords(project)
    assert "экран" in matching_project_keywords(project)
    assert "приложени" in matching_project_keywords(project)
