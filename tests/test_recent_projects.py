from freelance_bot.main import _latest_five
from freelance_bot.models import Project


def test_latest_five_returns_only_five_newest_projects() -> None:
    projects = [
        Project(
            source="Kwork",
            external_id=str(project_id),
            title=f"Проект {project_id}",
            description="",
            price="",
            url=f"https://example.com/{project_id}",
            category="Дизайн",
        )
        for project_id in range(1, 8)
    ]

    assert [project.external_id for project in _latest_five(projects)] == ["7", "6", "5", "4", "3"]
