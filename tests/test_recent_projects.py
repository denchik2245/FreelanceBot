import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from freelance_bot.main import _latest_five, _latest_suitable_projects, _listen_for_commands
from freelance_bot.models import AiAssessment, Project
from freelance_bot.project_filters import ProjectFilterManager
from freelance_bot.storage import ProjectStore
from freelance_bot.vk import COMMAND_PROFI, BotCommand


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
            published_at=datetime(2026, 7, project_id, tzinfo=UTC),
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
                summary=f"Кратко о проекте {project.external_id}",
            )

    store = ProjectStore(tmp_path / "projects.sqlite3")
    advisor = FakeAdvisor()
    try:
        selected = await _latest_suitable_projects(projects, store, advisor)  # type: ignore[arg-type]
    finally:
        store.close()

    assert [project.external_id for project, _ in selected] == ["8", "6", "4", "2", "1"]
    assert advisor.assessed_ids == ["8", "7", "6", "5", "4", "3", "2", "1"]


@pytest.mark.asyncio
async def test_profi_manual_command_sends_five_and_preserves_seen_history(tmp_path):
    projects = [
        Project(
            source="Profi.ru",
            external_id=str(i),
            title=f"Задача {i}",
            description="Из сохранённого фильтра аккаунта",
            price="",
            url=f"https://profi.ru/{i}",
            category="Услуги аккаунта",
        )
        for i in range(1, 9)
    ]
    source = AsyncMock()
    source.fetch_for_manual_selection.return_value = projects
    bot = AsyncMock()

    async def listen(handler):
        await handler(BotCommand(COMMAND_PROFI))

    bot.listen.side_effect = listen
    advisor = AsyncMock()

    async def assess(project):
        return AiAssessment(
            project_key=project.key,
            suitable=project.external_id != "7",
            score=10 if project.external_id == "7" else 85,
            reason="Тест",
            response_text="",
            filter_model="test",
            response_model="",
            summary="Описание задачи",
        )

    advisor.assess.side_effect = assess
    store = ProjectStore(tmp_path / "manual.sqlite3")
    try:
        await _listen_for_commands(
            bot,
            store,
            None,
            None,
            source,
            asyncio.Lock(),
            asyncio.Lock(),
            asyncio.Lock(),
            advisor,
            AsyncMock(),
            ProjectFilterManager(store, default_ai_score=70),
        )
        source.fetch_for_manual_selection.assert_awaited_once()
        assert [call.args[0].external_id for call in bot.send.call_args_list] == [
            "3",
            "4",
            "5",
            "6",
            "8",
        ]
        assert all(call.kwargs["test_view"] for call in bot.send.call_args_list)
        assert all(not store.is_seen(project.key) for project in projects)
        assert not store.is_source_initialized("Profi.ru")
    finally:
        store.close()
