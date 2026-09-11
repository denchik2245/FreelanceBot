import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from freelance_bot.ai import GigaChatProjectAdvisor, _parse_filter_result, _project_context
from freelance_bot.models import Project


def _response(text: str, model: str) -> SimpleNamespace:
    if model == "GigaChat-2-Max" and not text.lstrip().startswith(("{", "```")):
        text = json.dumps({"body": text, "case_ids": []}, ensure_ascii=False)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        model=model,
    )


class FakeClient:
    def __init__(self, model: str, *responses: str | Exception) -> None:
        self.model = model
        self.responses = list(responses)
        self.requests: list[object] = []

    async def achat(self, request: object) -> SimpleNamespace:
        self.requests.append(request.model_copy(deep=True))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _response(response, self.model)


def _project() -> Project:
    return Project(
        source="Kwork",
        external_id="42",
        title="Дизайн лендинга",
        description="Нужен макет лендинга в Figma",
        price="до 50 000 ₽",
        url="https://example.com/42",
        category="Дизайн",
        published_at=datetime(2026, 7, 27, tzinfo=UTC),
    )


def _advisor(score: int) -> tuple[GigaChatProjectAdvisor, FakeClient, FakeClient]:
    filter_client = FakeClient(
        "GigaChat-2",
        json.dumps({
            "decision": "accept" if score >= 70 else "reject",
            "evidence": "Нужен макет лендинга в Figma",
            "reason": "Подходит",
            "summary": "Клиенту нужен макет лендинга в Figma.",
        }, ensure_ascii=False),
    )
    response_client = FakeClient(
        "GigaChat-2-Max",
        "Здравствуйте! Готов обсудить задачу и детали макета.",
        "Добрый день! Предлагаю начать с логики главной страницы.",
    )
    advisor = object.__new__(GigaChatProjectAdvisor)
    advisor._filter_client = filter_client
    advisor._response_client = response_client
    advisor._filter_model = "GigaChat-2"
    advisor._response_model = "GigaChat-2-Max"
    advisor._min_score = 70
    advisor._profile = "Веб-дизайнер, работаю в Figma."
    advisor._filter_prompt = "Оцени проект и верни JSON."
    advisor._response_prompt = "Напиши персональный отклик."
    advisor._stack = None
    return advisor, filter_client, response_client


def test_parse_filter_result_accepts_json_code_block() -> None:
    assert _parse_filter_result(
        '```json\n{"decision": "accept", "evidence": "UI", "reason": " Нужен UI ", '
        '"summary": " Нужен интерфейс сервиса "}\n```'
    ) == (
        "accept",
        "UI",
        "Нужен UI",
        "Нужен интерфейс сервиса",
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
    assert assessment.score == 100
    assert assessment.summary == "Клиенту нужен макет лендинга в Figma."
    assert assessment.response_model == ""
    assert assessment.response_text == ""
    assert assessment.filter_model == "GigaChat-2"
    assert len(filter_client.requests) == 1
    assert filter_client.requests[0].response_format is not None
    assert filter_client.requests[0].model == "GigaChat-2"
    assert response_client.requests == []


@pytest.mark.asyncio
async def test_advisor_skips_response_for_unsuitable_project() -> None:
    advisor, filter_client, response_client = _advisor(25)
    assessment = await advisor.assess(_project())

    assert not assessment.suitable
    assert assessment.response_text == ""
    assert assessment.response_model == ""
    assert len(filter_client.requests) == 1
    assert response_client.requests == []


@pytest.mark.asyncio
async def test_advisor_generates_response_on_demand() -> None:
    advisor, filter_client, response_client = _advisor(25)
    response_text = await advisor.generate_response(_project())

    assert "Готов обсудить" in response_text
    assert filter_client.requests == []
    assert len(response_client.requests) == 1
    assert response_client.requests[0].model == "GigaChat-2-Max"

    regenerated = await advisor.generate_response(_project(), previous_response=response_text)
    assert regenerated != response_text
    assert "Предлагаю начать" in regenerated
    assert len(response_client.requests) == 2
    second_messages = response_client.requests[1].messages
    assert response_text in second_messages[1].content
    assert "повторная генерация" in second_messages[0].content


@pytest.mark.asyncio
async def test_advisor_rewrites_response_with_selected_style() -> None:
    advisor, _, response_client = _advisor(25)

    await advisor.generate_response(
        _project(),
        previous_response="Здравствуйте! Готов обсудить проект подробно.",
        variation="shorter",
    )

    system_prompt = response_client.requests[0].messages[0].content
    assert "заметно короче" in system_prompt
    assert "верни только готовый отклик" in system_prompt


@pytest.mark.asyncio
async def test_advisor_rejects_unknown_response_variation() -> None:
    advisor, _, _ = _advisor(25)

    with pytest.raises(ValueError, match="Неизвестный вариант"):
        await advisor.generate_response(
            _project(),
            previous_response="Предыдущий отклик",
            variation="sarcastic",
        )


@pytest.mark.asyncio
async def test_advisor_retries_invalid_gigachat_response(monkeypatch: pytest.MonkeyPatch) -> None:
    advisor, filter_client, _ = _advisor(85)
    filter_client.responses.insert(0, RuntimeError("пустой ответ"))

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("freelance_bot.ai.asyncio.sleep", no_sleep)
    assessment = await advisor.assess(_project())

    assert assessment.score == 100
    assert len(filter_client.requests) == 2


@pytest.mark.asyncio
async def test_advisor_falls_back_to_response_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisor, filter_client, response_client = _advisor(85)
    filter_client.responses = [RuntimeError("empty response")] * 3
    response_client.responses = [
        (
            '{"decision": "reject", "evidence": "", "reason": "Только программирование", '
            '"summary": "Клиенту нужна техническая доработка сайта."}'
        )
    ]

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("freelance_bot.ai.asyncio.sleep", no_sleep)
    assessment = await advisor.assess(_project())

    assert not assessment.suitable
    assert assessment.score == 0
    assert assessment.filter_model == "GigaChat-2-Max"
    assert len(filter_client.requests) == 3
    assert all(request.model == "GigaChat-2" for request in filter_client.requests)
    assert len(response_client.requests) == 1
    assert response_client.requests[0].model == "GigaChat-2-Max"


def test_filter_revision_tracks_selection_rules_only() -> None:
    advisor, _, _ = _advisor(85)
    original = advisor.filter_revision
    advisor.update_config_text("response", "Другой стиль отклика")
    advisor.set_min_score(80)
    assert advisor.filter_revision == original
    advisor.update_config_text("filter", "Исключить email-письма")
    revised = advisor.filter_revision
    assert revised != original
    advisor.update_config_text("profile", "Дизайн мобильных приложений")
    assert advisor.filter_revision == revised


@pytest.mark.asyncio
async def test_assessment_keeps_rules_used_before_live_edit() -> None:
    advisor, client, _ = _advisor(85)
    original = advisor.filter_revision
    original_achat = client.achat

    async def edit_during_request(request: object) -> SimpleNamespace:
        advisor.update_config_text("filter", "Новые критерии во время запроса")
        return await original_achat(request)

    client.achat = edit_during_request
    assessment = await advisor.assess(_project())
    assert assessment.filter_revision == original
    assert assessment.filter_revision != advisor.filter_revision
