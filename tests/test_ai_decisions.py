import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_ai import _advisor, _project

from freelance_bot.ai import (
    _parse_filter_result,
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


async def test_clear_title_can_pass_without_description_and_is_verified() -> None:
    advisor, primary, verifier = _advisor(85)
    advisor._verify_accepted = True
    verifier.responses = [json.dumps({
        "decision": "accept", "reason": "Дизайн лендинга", "summary": "Нужен дизайн лендинга.",
    })]
    assessment = await advisor.assess(replace(_project(), description=""))
    assert assessment.suitable
    for client in (primary, verifier):
        assert len(client.requests) == 1
        assert "Дизайн лендинга" in client.requests[0].messages[1].content


async def test_missing_title_and_description_cannot_pass_on_category_alone() -> None:
    advisor, _, _ = _advisor(85)
    assessment = await advisor.assess(replace(_project(), title=" ", description="<p>&nbsp;</p>"))
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


async def test_long_description_can_pass_and_reaches_both_models_in_full() -> None:
    advisor, primary, verifier = _advisor(85)
    advisor._verify_accepted = True
    description = (_project().description + " Описание разделов." * 600
                   + " В КОНЦЕ: сайт на Tilda со стандартной формой.")
    assert len(description) > 6000
    verifier.responses = [json.dumps({
        "decision": "accept", "reason": "Нужен сайт на Tilda", "summary": "Собрать сайт на Tilda.",
    })]
    project = replace(_project(), description=description)

    assert (await advisor.assess(project)).decision == "accept"
    for client in (primary, verifier):
        assert len(client.requests) == 1
        assert description in client.requests[0].messages[1].content


async def test_rejecting_requirements_at_end_of_long_description_reach_model() -> None:
    advisor, primary, verifier = _advisor(25)
    description = ("Разработать сайт. " + "Описание разделов и материалов. " * 300
                   + "В КОНЦЕ: собственный backend и личный кабинет с ролями.")
    project = replace(_project(), description=description)

    assert (await advisor.assess(project)).decision == "reject"
    assert description in primary.requests[0].messages[1].content
    assert verifier.requests == []


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
