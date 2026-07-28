from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from freelance_bot.ai import GigaChatProjectAdvisor, _parse_filter_result, _project_context
from freelance_bot.models import Project


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class FakeClient:
    def __init__(self, *responses: str | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []

    async def achat(self, request: object) -> SimpleNamespace:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _response(response)


def _project() -> Project:
    return Project(
        source="Kwork",
        external_id="42",
        title="Дизайн лендинга",
        description="Нужен макет лендинга в Figma",
        price="до 50 000 ₽",
        url="https://example.com/42",
        category="Дизайн",
        published_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )


def _advisor(score: int) -> tuple[GigaChatProjectAdvisor, FakeClient, FakeClient]:
    filter_client = FakeClient(f'```json\n{{"score": {score}, "reason": "Подходит"}}\n```')
    response_client = FakeClient(
        "Здравствуйте! Готов обсудить задачу и детали макета.",
        "Добрый день! Предлагаю начать с логики главной страницы.",
    )
    advisor = object.__new__(GigaChatProjectAdvisor)
    advisor._filter_client = filter_client
    advisor._response_client = response_client
    advisor._filter_model = "GigaChat-2"
    advisor._response_model = "GigaChat-2-Pro"
    advisor._min_score = 70
    advisor._profile = "Веб-дизайнер, работаю в Figma."
    advisor._filter_prompt = "Оцени проект и верни JSON."
    advisor._response_prompt = "Напиши персональный отклик."
    advisor._stack = None
    return advisor, filter_client, response_client


def test_parse_filter_result_accepts_json_code_block() -> None:
    assert _parse_filter_result('```json\n{"score": 81, "reason": " Нужен UI "}\n```') == (
        81,
        "Нужен UI",
    )


def test_project_context_converts_description_html_to_plain_text() -> None:
    project = _project()
    project = Project(
        source=project.source,
        external_id=project.external_id,
        title=project.title,
        description="Нужен <b>макет</b><br>в Figma &amp; Tilda",
        price=project.price,
        url=project.url,
        category=project.category,
        published_at=project.published_at,
    )

    context = _project_context(project)

    assert "Описание: Нужен макет в Figma & Tilda" in context
    assert "<b>" not in context


@pytest.mark.asyncio
async def test_advisor_only_assesses_suitable_project() -> None:
    advisor, filter_client, response_client = _advisor(85)
    assessment = await advisor.assess(_project())

    assert assessment.suitable
    assert assessment.score == 85
    assert assessment.response_model == ""
    assert assessment.response_text == ""
    assert len(filter_client.requests) == 1
    assert getattr(filter_client.requests[0], "response_format") is not None
    assert response_client.requests == []


@pytest.mark.asyncio
async def test_advisor_skips_response_for_unsuitable_project() -> None:
    advisor, _, response_client = _advisor(25)
    assessment = await advisor.assess(_project())

    assert not assessment.suitable
    assert assessment.response_text == ""
    assert assessment.response_model == ""
    assert response_client.requests == []


@pytest.mark.asyncio
async def test_advisor_generates_response_on_demand() -> None:
    advisor, _, response_client = _advisor(25)
    response_text = await advisor.generate_response(_project())

    assert "Готов обсудить" in response_text
    assert len(response_client.requests) == 1

    regenerated = await advisor.generate_response(_project(), previous_response=response_text)
    assert regenerated != response_text
    assert "Предлагаю начать" in regenerated
    assert len(response_client.requests) == 2
    second_messages = getattr(response_client.requests[1], "messages")
    assert response_text in second_messages[1].content
    assert "повторная генерация" in second_messages[0].content


@pytest.mark.asyncio
async def test_advisor_retries_invalid_gigachat_response(monkeypatch: pytest.MonkeyPatch) -> None:
    advisor, filter_client, _ = _advisor(85)
    filter_client.responses.insert(0, RuntimeError("пустой ответ"))

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("freelance_bot.ai.asyncio.sleep", no_sleep)
    assessment = await advisor.assess(_project())

    assert assessment.score == 85
    assert len(filter_client.requests) == 2


@pytest.mark.asyncio
async def test_advisor_falls_back_to_response_model(monkeypatch: pytest.MonkeyPatch) -> None:
    advisor, filter_client, response_client = _advisor(85)
    filter_client.responses = [RuntimeError("empty response")] * 3
    response_client.responses = ['{"score": 15, "reason": "Только программирование"}']

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("freelance_bot.ai.asyncio.sleep", no_sleep)
    assessment = await advisor.assess(_project())

    assert not assessment.suitable
    assert assessment.score == 15
    assert assessment.filter_model == "GigaChat-2-Pro"
    assert len(filter_client.requests) == 3
    assert len(response_client.requests) == 1
