import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest
from gigachat.models import Chat, Messages, MessagesRole
from test_ai import _project
from test_config import _base_environment

from freelance_bot.ai import (
    FILTER_RESPONSE_FORMAT,
    DeepSeekProjectAdvisor,
    GigaChatProjectAdvisor,
    create_advisor,
)
from freelance_bot.config import Settings
from freelance_bot.deepseek import DeepSeekApiError, DeepSeekClient
from freelance_bot.response_skill import load_response_skill


def _reply(content: str, finish: str = "stop") -> dict:
    return {
        "model": "deepseek-flash",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 80},
    }


class FakeResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status = status
        self.read = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        self.read = True
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


@pytest.fixture
def transport(monkeypatch):
    responses = []
    requests = []
    sessions = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.closed = False
            sessions.append(self)

        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        async def close(self):
            self.closed = True

    monkeypatch.setattr("freelance_bot.deepseek.aiohttp.ClientSession", FakeSession)
    return responses, requests, sessions


async def test_json_request_uses_direct_api_without_thinking(transport, caplog):
    responses, requests, sessions = transport
    responses.append(FakeResponse(_reply('{"decision":"reject","reason":"Логотип","summary":""}')))
    request = Chat(
        model="deepseek-flash", temperature=.1, max_tokens=500,
        response_format=FILTER_RESPONSE_FORMAT,
        messages=[Messages(role=MessagesRole.SYSTEM, content="Оцени заказ.")],
    )
    original = request.model_dump()
    with caplog.at_level(logging.INFO):
        async with DeepSeekClient(
            api_key="private-test-key", base_url="https://api.deepseek.com/", model="deepseek-flash",
        ) as client:
            reply = await client.achat(request)
    url, options = requests[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert options["allow_redirects"] is False
    payload = options["json"]
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 500
    assert "JSON" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"] == "Оцени заказ."
    assert "json_schema" not in json.dumps(payload)
    assert request.model_dump() == original
    assert reply.model == "deepseek-flash"
    assert reply.choices[0].finish_reason == "stop"
    assert sessions[0].options["headers"]["Authorization"] == "Bearer private-test-key"
    assert sessions[0].closed
    assert "вход=100, выход=20, кешированный вход=80" in caplog.text
    assert "private-test-key" not in caplog.text


@pytest.mark.parametrize("status", [401, 402, 429, 500, 302])
async def test_api_failure_hides_response_body_and_closes_session(transport, status):
    responses, _, sessions = transport
    response = FakeResponse({"error": "private-test-key"}, status=status)
    responses.append(response)
    with pytest.raises(DeepSeekApiError) as error:
        async with DeepSeekClient(
            api_key="private-test-key", base_url="https://api.deepseek.com", model="deepseek-flash",
        ) as client:
            await client.achat(Chat(model="deepseek-flash", messages=[]))
    assert error.value.status == status
    assert "private-test-key" not in str(error.value)
    assert not response.read
    assert sessions[0].closed


@pytest.mark.parametrize("data", [
    ValueError("private response"), [], {}, {"choices": []},
    {"choices": [{"message": {"content": None}}]},
    _reply('{"decision":"accept","reason":"Да","summary":"Макет"}',
           "insufficient_system_resource"),
])
async def test_malformed_api_responses_fail(transport, data):
    transport[0].append(FakeResponse(data))
    async with DeepSeekClient(
        api_key="test-key", base_url="https://api.deepseek.com", model="deepseek-flash",
    ) as client:
        with pytest.raises(RuntimeError):
            await client.achat(Chat(model="deepseek-flash", messages=[]))


@pytest.mark.parametrize("failure", [
    aiohttp.ClientConnectionError("private-test-key"), asyncio.TimeoutError(),
])
async def test_connection_errors_are_sanitized(transport, failure):
    transport[0].append(failure)
    async with DeepSeekClient(
        api_key="test-key", base_url="https://api.deepseek.com", model="deepseek-flash",
    ) as client:
        with pytest.raises(RuntimeError, match="соединиться") as error:
            await client.achat(Chat(model="deepseek-flash", messages=[]))
    assert "private-test-key" not in str(error.value)
    assert error.value.__suppress_context__


def test_factory_selects_provider_and_tracks_revision(monkeypatch):
    _base_environment(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    settings = Settings.from_env()
    advisor = create_advisor(settings)
    assert isinstance(advisor, DeepSeekProjectAdvisor)
    assert isinstance(advisor._filter_client, DeepSeekClient)
    assert advisor.filter_model == advisor.response_model == "deepseek-flash"
    giga = create_advisor(replace(
        settings, ai_provider="gigachat", gigachat_credentials="test-key",
        gigachat_filter_model="deepseek-flash", gigachat_filter_fallback_model="deepseek-flash",
    ))
    assert isinstance(giga, GigaChatProjectAdvisor)
    assert advisor.filter_revision != giga.filter_revision


def _advisor():
    return DeepSeekProjectAdvisor(
        credentials="test-key", scope="", base_url="https://api.deepseek.com",
        ca_bundle_file=None, filter_model="deepseek-flash",
        filter_fallback_model="deepseek-flash", response_model="deepseek-flash",
        min_score=70, profile="Веб-дизайнер, работаю в Figma.",
        filter_prompt="Оцени проект и верни JSON.", response_prompt="Напиши персональный отклик.",
    )


async def test_advisor_accepts_and_writes_without_duplicate_verification(transport):
    responses, requests, sessions = transport
    responses.extend([
        FakeResponse(_reply(json.dumps({
            "decision": "accept", "reason": "Нужен макет", "summary": "Макет лендинга в Figma.",
        }))),
        FakeResponse(_reply(json.dumps({
            "body": "Здравствуйте! Предложу структуру и подготовлю макет лендинга в Figma.",
            "case_ids": [],
        }))),
    ])
    async with _advisor() as advisor:
        assessment = await advisor.assess(_project())
        assert assessment.decision == "accept"
        assert len(requests) == 1
        body = await advisor.generate_response(_project())
        assert body.startswith("Здравствуйте!")
    assert len(requests) == 2
    assert all(session.closed for session in sessions)


@pytest.mark.parametrize("content,finish", [
    ('{"decision":"accept","reason":"Да","summary":"Макет"}', "length"),
    ('{"decision":"maybe","reason":"Да","summary":"Макет"}', "stop"),
])
async def test_bad_decision_cannot_be_accepted(transport, monkeypatch, content, finish):
    async def no_wait(*args):
        pass

    monkeypatch.setattr("freelance_bot.ai.asyncio.sleep", no_wait)
    responses, requests, _ = transport
    responses.extend(FakeResponse(_reply(content, finish)) for _ in range(3))
    async with _advisor() as advisor:
        with pytest.raises(ValueError):
            await advisor.assess(_project())
    assert len(requests) == 3


@pytest.mark.parametrize("source", ["Kwork", "FL.ru", "Profi.ru"])
async def test_local_skill_reaches_deepseek_and_renders_platform_links(
    transport, monkeypatch, source,
):
    _base_environment(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    advisor = create_advisor(Settings.from_env())
    project = replace(
        _project(), source=source, title="Магазин электроники на Tilda",
        description="Каталог техники по категориям, карточки товаров, корзина и Trade-In.",
    )
    responses, requests, _ = transport
    responses.extend(FakeResponse(_reply(json.dumps({
        "body": "Здравствуйте! Разделю каталог по категориям техники.\n\n"
                "{{CASE:electronics-store-tilda}}\n\n"
                "Этот проект поможет согласовать подачу условий покупки.\n\n"
                "Пришлите перечень категорий и материалы для карточек.",
        "case_ids": ["electronics-store-tilda"],
    }))) for _ in range(2))
    async with advisor:
        body = await advisor.generate_response(project)
        rewritten = await advisor.generate_response(
            project, previous_response=body, variation="shorter",
        )

    skill = Path("config/skills/freelance-response")
    for _, options in requests:
        system = options["json"]["messages"][1]["content"]
        assert (skill / "SKILL.md").read_text(encoding="utf-8").strip() in system
        assert (skill / "references/writing-style.md").read_text(encoding="utf-8").strip() in system
        assert advisor._profile in system
        assert "verified_card" in system
        assert "Не утверждать" in system
        assert "talantum-exam-courses" not in system
        assert "https://" not in system.split("КЕЙСЫ-КАНДИДАТЫ", 1)[1]
    assert "повторная генерация" in requests[1][1]["json"]["messages"][1]["content"]
    assert requests[0][1]["json"]["model"] == advisor.response_model
    for text in (body, rewritten):
        assert "{{CASE:" not in text
        if source == "FL.ru":
            assert "https://www.fl.ru/user/flarich2281337/portfolio/8086703/" in text
            assert "https://kwork.ru/portfolio/" not in text
            assert text.count("Telegram:") == text.count("MAX:") == 1
            assert text.endswith("X2alwQ7o")
        else:
            assert "https://kwork.ru/portfolio/24001122" in text
            assert "Telegram:" not in text and "MAX:" not in text


def test_missing_style_does_not_silently_disable_skill(tmp_path):
    (tmp_path / "SKILL.md").write_text("Правила отклика", encoding="utf-8")
    with pytest.raises(RuntimeError, match="writing-style.md"):
        load_response_skill(tmp_path)


def test_empty_skill_is_rejected(tmp_path):
    (tmp_path / "SKILL.md").write_text(" \n", encoding="utf-8")
    with pytest.raises(ValueError, match="пусты"):
        load_response_skill(tmp_path)
