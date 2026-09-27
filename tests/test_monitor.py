from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from freelance_bot import main
from freelance_bot.models import AiAssessment, Project
from freelance_bot.project_filters import ProjectFilterManager
from freelance_bot.storage import ProjectStore


class EndCycle(Exception):
    pass


@pytest.mark.parametrize("legacy_seen", [False, True])
async def test_unclear_is_rechecked_only_when_project_changes(
    tmp_path, monkeypatch, legacy_seen: bool
) -> None:
    project = Project(
        source="Kwork", external_id="42", title="Сделать сайт на Тильде",
        description="", price="до 20 000 ₽", url="https://kwork.ru/projects/42/view",
        category="Сайты", published_at=datetime.now(UTC),
    )
    current = project
    assessments: list[str] = []
    sent: list[str] = []

    class Advisor:
        filter_revision = "test-rules"

        async def assess(self, item: Project) -> AiAssessment:
            assessments.append(item.description)
            decision = "accept" if item.description else "unclear"
            return AiAssessment(
                project_key=item.key, suitable=decision == "accept",
                score=100 if decision == "accept" else 0,
                reason="Описание получено" if item.description else "Нет описания",
                response_text="", filter_model="Pro", response_model="",
                summary="Сайт на Tilda" if item.description else "",
                filter_revision=self.filter_revision, decision=decision,
            )

    class Bot:
        async def send(self, item: Project, *, assessment: AiAssessment) -> None:
            sent.append(item.key)

    async def fetch(*_args):
        return {"Kwork": [current]}

    async def end_cycle(_seconds):
        raise EndCycle

    monkeypatch.setattr(main, "_fetch_sources", fetch)
    monkeypatch.setattr(main, "asyncio", SimpleNamespace(sleep=end_cycle))
    store = ProjectStore(tmp_path / "projects.sqlite3")
    store.mark_source_initialized("Kwork")
    advisor = Advisor()

    async def cycle() -> None:
        with pytest.raises(EndCycle):
            await main._monitor_projects(
                SimpleNamespace(send_existing_on_first_run=False, poll_interval_seconds=1),
                store, None, None, None, None, None, None, Bot(), advisor,
                ProjectFilterManager(store, default_ai_score=70),
            )

    try:
        await cycle()
        assert assessments == [""]
        assert not store.is_seen(project.key)
        if legacy_seen:
            store.mark_seen(project.key, project.source)
        await cycle()
        assert assessments == [""]
        advisor.filter_revision = "updated-rules"
        await cycle()
        assert assessments == ["", ""]
        current = replace(project, description="Нужны главная и страница услуг на Tilda")
        await cycle()
        assert assessments == ["", "", current.description]
        assert sent == [project.key]
        assert store.is_seen(project.key)
    finally:
        store.close()
