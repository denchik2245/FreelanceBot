import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_ai import _advisor, _project, _response

from freelance_bot.ai import _parse_filter_result
from freelance_bot.config_texts import ConfigTextManager
from freelance_bot.portfolio import (
    parse_portfolio,
    requested_case_count,
    response_issues,
    select_cases,
)
from freelance_bot.storage import ProjectStore


@pytest.mark.parametrize("decision", ["reject", "unclear"])
async def test_nonaccepted_decisions_need_no_summary(decision: str) -> None:
    advisor, client, _ = _advisor(85)
    client.responses = [json.dumps({
        "decision": decision, "evidence": "", "reason": "Недостаточно данных", "summary": ""
    })]
    assessment = await advisor.assess(_project())
    assert assessment.decision == decision
    assert not assessment.suitable
    assert assessment.summary == ""


@pytest.mark.parametrize("payload", [
    {"score": 95, "reason": "yes", "summary": "yes"},
    {"decision": "maybe", "evidence": "UI", "reason": "yes", "summary": "yes"},
    {"decision": "accept", "evidence": "", "reason": "yes", "summary": "yes"},
    {"decision": "accept", "evidence": "UI", "reason": True, "summary": "yes"},
])
def test_invalid_decisions_fail_closed(payload: dict) -> None:
    with pytest.raises(ValueError):
        _parse_filter_result(json.dumps(payload))


async def test_filter_does_not_receive_profile_and_ignores_legacy_threshold() -> None:
    advisor, client, _ = _advisor(85)
    advisor._profile = "SECRET PROFILE https://portfolio.example"
    advisor.set_min_score(0)
    assessment = await advisor.assess(_project())
    assert assessment.decision == "accept"
    assert assessment.evidence == "Нужен макет лендинга в Figma"
    assert "SECRET PROFILE" not in str(client.requests[0].messages)


@pytest.mark.parametrize("description", ["", "Нужен логотип"])
async def test_missing_description_or_fabricated_evidence_cannot_pass(description: str) -> None:
    advisor, _, _ = _advisor(85)
    assessment = await advisor.assess(replace(_project(), description=description))
    assert assessment.decision == "unclear"
    assert not assessment.suitable


def test_decision_and_evidence_survive_database_reopen(tmp_path: Path) -> None:
    from freelance_bot.models import AiAssessment

    path = tmp_path / "db.sqlite3"
    store = ProjectStore(path)
    store.remember_ai_assessment(AiAssessment(
        project_key="Kwork:42", suitable=False, score=0, reason="Неясно",
        response_text="", filter_model="GigaChat-2", response_model="",
        decision="unclear", evidence="Сделать сайт",
    ))
    store.close()
    store = ProjectStore(path)
    try:
        assessment = store.get_ai_assessment("Kwork:42")
        assert assessment.decision == "unclear"
        assert assessment.evidence == "Сделать сайт"
    finally:
        store.close()


async def test_writer_repairs_unapproved_link_once() -> None:
    advisor, _, client = _advisor(85)
    client.responses = ["Мой кейс https://invented.example/", "Здравствуйте! Подготовлю макет."]
    text = await advisor.generate_response(_project())
    assert "invented" not in text
    assert len(client.requests) == 2
    assert "разрешённые ссылки" in client.requests[1].messages[-1].content


async def test_writer_rejects_repeated_invalid_output() -> None:
    advisor, _, client = _advisor(85)
    client.responses = ["https://invented.example/", "https://invented.example/"]
    with pytest.raises(ValueError, match="не прошёл проверку"):
        await advisor.generate_response(_project())
    assert len(client.requests) == 2


async def test_writer_repairs_truncated_output() -> None:
    advisor, _, client = _advisor(85)
    calls = []

    async def achat(request):
        calls.append(request.model_copy(deep=True))
        response = _response("Здравствуйте! Подготовлю макет.", "GigaChat-2-Max")
        response.choices[0].finish_reason = "length" if len(calls) == 1 else "stop"
        return response

    client.achat = achat
    await advisor.generate_response(_project())
    assert len(calls) == 2
    assert "обрезан" in calls[1].messages[-1].content


def test_response_links_and_empty_text() -> None:
    assert not response_issues("Кейс: https://example.com/a.", {"https://example.com/a"}, "stop")
    assert response_issues(" ", set(), "stop")
    assert response_issues("https://example.com/a/extra", {"https://example.com/a"}, "stop")
    assert response_issues("example.com/a", {"https://example.com/a"}, "stop")
    assert response_issues("Изучил текущую версию сайта", set(), "stop")
    assert not response_issues("Изучил ваш запрос", set(), "stop")


def test_portfolio_tilda_candidates_and_no_arbitrary_fallback() -> None:
    cases = parse_portfolio(Path("config/portfolio.json").read_text(encoding="utf-8"))
    selected = select_cases(replace(_project(), description="Сборка на Tilda"), cases)
    assert all(case["platform"] == "Tilda" for case in selected[:3])
    assert not select_cases(replace(_project(), title="xyz", description="abc"), cases)
    mobile_cases = select_cases(replace(_project(), description="Дизайн приложения"), cases)
    assert all("веб-сервис" in case["name"].lower() for case in mobile_cases)
    medical_cases = select_cases(replace(
        _project(), title="Страница стоматологии", description="Дизайн страницы стоматологии"
    ), cases)
    assert "стоматолог" in medical_cases[0]["name"]
    landing_cases = select_cases(replace(
        _project(), description="Дизайн лендинга курсов. Приложите два примера."
    ), cases)
    assert len(landing_cases) >= 2
    assert "лендинг" in landing_cases[0]["name"]


def test_invalid_portfolio_edit_keeps_original(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.json"
    path.write_text("[]", encoding="utf-8")
    manager = ConfigTextManager(
        profile_path=tmp_path / "profile.txt", filter_prompt_path=tmp_path / "filter.txt",
        response_prompt_path=tmp_path / "response.txt", portfolio_path=path,
    )
    with pytest.raises(ValueError):
        manager.replace("portfolio", '[{"url":"https://example.com"}]')
    assert path.read_text(encoding="utf-8") == "[]"


def test_portfolio_roundtrip_live_edit() -> None:
    advisor, _, _ = _advisor(85)
    advisor.update_config_text("portfolio", "[]")
    assert advisor._portfolio == []


async def test_verifier_can_reject_lite_accept_without_receiving_its_reason() -> None:
    advisor, _, verifier = _advisor(85)
    advisor._verify_accepted = True
    verifier.responses = [json.dumps({
        "decision": "reject", "evidence": "", "reason": "Это заставка", "summary": ""
    })]
    assessment = await advisor.assess(_project())
    assert assessment.decision == "reject"
    assert assessment.filter_model == "GigaChat-2-Max"
    assert len(verifier.requests) == 1
    assert "Подходит" not in str(verifier.requests[0].messages)


async def test_lite_rejection_does_not_spend_verifier_call() -> None:
    advisor, _, verifier = _advisor(25)
    advisor._verify_accepted = True
    assert (await advisor.assess(_project())).decision == "reject"
    assert verifier.requests == []


async def test_verifier_failure_never_sends_lite_accept(monkeypatch) -> None:
    advisor, _, verifier = _advisor(85)
    advisor._verify_accepted = True
    verifier.responses = [RuntimeError("offline")] * 3

    async def no_sleep(_):
        pass

    monkeypatch.setattr("freelance_bot.ai.asyncio.sleep", no_sleep)
    with pytest.raises(RuntimeError, match="offline"):
        await advisor.assess(_project())


async def test_truncated_input_cannot_pass() -> None:
    advisor, _, _ = _advisor(85)
    project = replace(_project(), description=_project().description + " x" * 4000)
    assert (await advisor.assess(project)).decision == "unclear"


async def test_quote_case_is_restored_from_source() -> None:
    advisor, client, _ = _advisor(85)
    payload = json.loads(client.responses[0])
    payload["evidence"] = payload["evidence"].lower()
    client.responses = [json.dumps(payload)]
    result = await advisor.assess(_project())
    assert result.decision == "accept"
    assert result.evidence == "Нужен макет лендинга в Figma"


async def test_writer_inserts_only_catalog_facts_and_keeps_next_step_last() -> None:
    advisor, _, client = _advisor(85)
    advisor._portfolio = [{
        "id": "known", "name": "Макет лендинга", "product": "лендинга",
        "work": "макет в Figma", "platform": "не указана", "url": "https://example.com/case",
    }]
    client.responses = [json.dumps({
        "body": "Здравствуйте! Подготовлю макет.\n\nПришлите структуру.", "case_ids": ["known"]
    })]
    text = await advisor.generate_response(_project())
    assert "Макет лендинга — макет в Figma\nhttps://example.com/case" in text
    assert text.endswith("Пришлите структуру.")
    assert "known" not in text
    assert "https://example.com/case" not in client.requests[0].messages[0].content


async def test_writer_unknown_case_id_requires_repair() -> None:
    advisor, _, client = _advisor(85)
    client.responses = [
        json.dumps({"body": "Здравствуйте! Подготовлю макет.", "case_ids": ["invented"]}),
        json.dumps({"body": "Здравствуйте! Подготовлю макет.", "case_ids": []}),
    ]
    assert await advisor.generate_response(_project()) == "Здравствуйте! Подготовлю макет."
    assert len(client.requests) == 2


def test_evaluation_metrics_distinguish_errors_and_misses() -> None:
    from freelance_bot.evaluate_ai import metrics

    result = metrics([
        {"expected": "reject", "decision": "accept"},
        {"expected": "accept", "decision": "unclear"},
        {"expected": "accept", "error": "ConnectError"},
    ])
    assert result["false_accepts"] == 1
    assert result["missed_accepts"] == 1
    assert result["errors"] == 1
    assert result["precision"] == 0
    assert result["recall"] == 0
    assert metrics([])["precision"] is None


@pytest.mark.parametrize(("description", "count"), [
    ("В отклике три примера реализованных сайтов", 3),
    ("Дайте один пример", 1),
    ("Без портфолио", 0),
    ("Дизайн трёх страниц", None),
])
def test_explicit_case_count(description: str, count: int | None) -> None:
    assert requested_case_count(replace(_project(), description=description)) == count


async def test_explicit_case_count_is_validated_even_when_model_ignores_schema() -> None:
    advisor, _, client = _advisor(85)
    advisor._portfolio = parse_portfolio(Path("config/portfolio.json").read_text(encoding="utf-8"))
    client.responses = [json.dumps({"body": "Здравствуйте", "case_ids": []})] * 2
    project = replace(_project(), description="Собрать на Tilda. В отклике три примера.")
    with pytest.raises(ValueError, match="не прошёл проверку"):
        await advisor.generate_response(project)
    assert len(client.requests) == 2
