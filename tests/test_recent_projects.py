from datetime import datetime, timezone

import pytest

from freelance_bot.main import _latest_five, _latest_suitable_projects
from freelance_bot.models import AiAssessment, Project
from freelance_bot.storage import ProjectStore


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


@pytest.mark.asyncio
async def test_latest_suitable_projects_scans_past_rejected_projects(tmp_path) -> None:
    projects = [
        Project(
            source="Kwork",
            external_id=str(project_id),
            title=f"Проект {project_id}",
            description=f"Описание {project_id}",
            price="",
            url=f"https://example.com/{project_id}",
            category="Дизайн",
            published_at=datetime(2026, 7, project_id, tzinfo=timezone.utc),
        )
        for project_id in range(1, 9)
    ]
    suitable_ids = {"8", "6", "4", "2", "1"}

    class FakeAdvisor:
        def __init__(self) -> None:
            self.assessed_ids: list[str] = []

        async def assess(self, project: Project) -> AiAssessment:
            self.assessed_ids.append(project.external_id)
            suitable = project.external_id in suitable_ids
            return AiAssessment(
                project_key=project.key,
                suitable=suitable,
                score=85 if suitable else 10,
                reason="Подходит" if suitable else "Не подходит",
                response_text="",
                filter_model="GigaChat-2-Pro",
                response_model="",
            )

    store = ProjectStore(tmp_path / "projects.sqlite3")
    advisor = FakeAdvisor()
    try:
        selected = await _latest_suitable_projects(projects, store, advisor)  # type: ignore[arg-type]
    finally:
        store.close()

    assert [project.external_id for project, _ in selected] == ["8", "6", "4", "2", "1"]
    assert advisor.assessed_ids == ["8", "7", "6", "5", "4", "3", "2", "1"]
