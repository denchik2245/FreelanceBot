import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_ai import _advisor, _project, _response

from freelance_bot.ai import (
    GigaChatProjectAdvisor,
    InsufficientPortfolioCasesError,
    _parse_filter_result,
)
from freelance_bot.config_texts import ConfigTextManager
from freelance_bot.portfolio import (
    load_portfolio_documents,
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
        "decision": decision, "reason": "Недостаточно данных", "summary": ""
    })]
    assessment = await advisor.assess(_project())
    assert assessment.decision == decision
    assert not assessment.suitable
    assert assessment.summary == ""


@pytest.mark.parametrize("payload", [
    {"score": 95, "reason": "yes", "summary": "yes"},
    {"decision": "maybe", "reason": "yes", "summary": "yes"},
    {"decision": "accept", "reason": "yes", "summary": ""},
    {"decision": "accept", "reason": True, "summary": "yes"},
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
    assert assessment.evidence == ""
    assert "SECRET PROFILE" not in str(client.requests[0].messages)


async def test_missing_description_cannot_pass() -> None:
    advisor, _, _ = _advisor(85)
    assessment = await advisor.assess(replace(_project(), description=""))
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
    assert response_issues("Задача — сделать макет", set(), "stop")
    assert response_issues("Здесь важно не просто оформить, а помочь выбрать", set(), "stop")
    assert response_issues("Готов обсудить ТЗ", set(), "stop")


def test_portfolio_tilda_candidates_and_no_arbitrary_fallback() -> None:
    cases = parse_portfolio(Path("config/portfolio.json").read_text(encoding="utf-8"))
    selected = select_cases(replace(_project(), description="Сборка на Tilda"), cases)
    assert all(case["platform"] == "Tilda" for case in selected[:3])
    assert not select_cases(replace(_project(), title="xyz", description="abc"), cases)
    mobile_cases = select_cases(replace(_project(), description="Дизайн приложения"), cases)
    assert mobile_cases == []
    medical_cases = select_cases(replace(
        _project(), title="Курсы первой помощи", description="Лендинг медицинского обучения"
    ), cases)
    assert medical_cases[0]["id"] == "first-aid-courses-tilda"
    landing_cases = select_cases(replace(
        _project(), description="Дизайн лендинга курсов. Приложите два примера."
    ), cases)
    assert len(landing_cases) >= 2
    assert landing_cases[0]["id"] == "first-aid-courses-tilda"


def test_full_portfolio_documents_match_catalog_and_hide_urls() -> None:
    path = Path("config/portfolio.json")
    cases = parse_portfolio(path.read_text(encoding="utf-8"))
    documents = load_portfolio_documents(path, cases)

    assert documents.keys() == {case["id"] for case in cases}
    assert "банкротство" in documents["legal-company-redesign"]["search_text"]
    assert "## Чего не утверждать" in documents["legal-company-redesign"]["card"]
    assert all("https://kwork.ru/portfolio/" not in doc["card"] for doc in documents.values())
    assert "Сайт из кейса:" not in documents["legal-company-redesign"]["card"]


def test_portfolio_documents_reject_stale_public_link() -> None:
    path = Path("config/portfolio.json")
    cases = parse_portfolio(path.read_text(encoding="utf-8"))
    cases[0]["url"] = "https://example.com/different"
    with pytest.raises(ValueError, match="публичная ссылка"):
        load_portfolio_documents(path, cases)


def test_advisor_loads_full_portfolio_and_guards_live_catalog_edits() -> None:
    advisor = GigaChatProjectAdvisor.from_paths(
        profile_path=Path("config/freelancer_profile.txt"),
        filter_prompt_path=Path("config/prompts/project_filter.txt"),
        response_prompt_path=Path("config/prompts/response_writer.txt"),
        portfolio_path=Path("config/portfolio.json"),
        credentials="test", scope="GIGACHAT_API_PERS",
        base_url="https://gigachat.devices.sberbank.ru/api/v1", ca_bundle_file=None,
        filter_model="GigaChat-2-Pro", filter_fallback_model="GigaChat-2-Max",
        response_model="GigaChat-3-Ultra", min_score=0,
    )
    assert len(advisor._portfolio_documents) == 10
    changed = [dict(case) for case in advisor._portfolio]
    changed[0]["url"] = "https://example.com/not-verified"
    with pytest.raises(ValueError, match="Markdown-карточками"):
        advisor.update_config_text("portfolio", json.dumps(changed))
    assert advisor._portfolio[0]["url"] != changed[0]["url"]


async def test_writer_receives_full_relevant_cards_only() -> None:
    advisor, _, client = _advisor(85)
    path = Path("config/portfolio.json")
    advisor._portfolio = parse_portfolio(path.read_text(encoding="utf-8"))
    advisor._portfolio_documents = load_portfolio_documents(path, advisor._portfolio)
    client.responses = [json.dumps({"body": "Здравствуйте! Подготовлю макеты.", "case_ids": []})]
    project = replace(
        _project(), title="Редизайн сайта юридической компании",
        description="Нужен многостраничный сайт про банкротство. Большой объем текста.",
    )

    await advisor.generate_response(project)

    prompt = client.requests[0].messages[0].content
    assert "## Чего не утверждать" in prompt
    assert "130 экранов" in prompt
    assert "hockey-event-tilda" not in prompt
    assert "https://kwork.ru/portfolio/" not in prompt


@pytest.mark.parametrize("exclusion", [
    "Tilda не подходит", "Tilda нам не подходит", "не нужна Tilda", "без Tilda",
])
def test_tilda_negation_does_not_boost_tilda_cases(exclusion: str) -> None:
    cases = parse_portfolio(Path("config/portfolio.json").read_text(encoding="utf-8"))
    project = replace(
        _project(),
        title="Дизайн сайта под WordPress",
        description=f"Только макет. {exclusion}; сайт собирает другая команда.",
    )
    assert select_cases(project, cases)[0]["platform"] != "Tilda"


@pytest.mark.parametrize("case_id", ["case_1", "Case-1", "case.1"])
def test_portfolio_rejects_ids_that_cannot_be_used_in_markers(case_id: str) -> None:
    case = {
        "id": case_id, "name": "Кейс", "product": "лендинг",
        "work": "Сделал макет", "platform": "Tilda", "url": "https://example.com/case",
    }
    with pytest.raises(ValueError, match="ID кейса"):
        parse_portfolio(json.dumps([case]))


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
        "decision": "reject", "reason": "Это заставка", "summary": ""
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


async def test_both_models_can_accept_tilda_project_without_literal_quote() -> None:
    advisor, primary, verifier = _advisor(85)
    advisor._verify_accepted = True
    project = replace(
        _project(),
        title="Сделать сайт на Тильде",
        description=(
            "Оценить создание главной страницы и страницы банкросство, "
            "остальные услуги пока просто на заявку вывести."
        ),
    )
    primary.responses = [json.dumps({
        "decision": "accept",
        "reason": "Нужен сайт на Tilda", "summary": "Создание страниц на Tilda",
    })]
    verifier.responses = [json.dumps({
        "decision": "accept", "reason": "Нужны две страницы на Tilda",
        "summary": "Создание страниц на Tilda",
    })]

    assessment = await advisor.assess(project)

    assert assessment.decision == "accept"
    assert assessment.evidence == ""
    assert len(primary.requests) == 1
    assert len(verifier.requests) == 1


async def test_writer_inserts_only_catalog_facts_and_keeps_next_step_last() -> None:
    advisor, _, client = _advisor(85)
    advisor._portfolio = [{
        "id": "known", "name": "Макет лендинга", "product": "лендинга",
        "work": "макет в Figma", "platform": "не указана", "url": "https://example.com/case",
    }]
    client.responses = [json.dumps({
        "body": (
            "Здравствуйте! Подготовлю макет.\n\n"
            "{{CASE:known}}\nЭтот опыт полезен для логики лендинга.\n\nПришлите структуру."
        ),
        "case_ids": ["known"],
    })]
    text = await advisor.generate_response(_project())
    assert "Макет лендинга: https://example.com/case\nмакет в Figma" in text
    assert "Этот опыт полезен для логики лендинга." in text
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


async def test_explicit_case_count_is_not_silently_reduced() -> None:
    advisor, _, client = _advisor(85)
    advisor._portfolio = [{
        "id": "landing", "name": "Лендинг", "product": "лендинг",
        "work": "Сделал макет", "platform": "не указана",
        "url": "https://example.com/landing",
    }]
    project = replace(_project(), description="Нужен лендинг. Приложите три примера работ.")
    with pytest.raises(InsufficientPortfolioCasesError, match="просит 3 примера"):
        await advisor.generate_response(project)
    assert client.requests == []
