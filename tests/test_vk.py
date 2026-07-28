import json
from datetime import datetime, timezone

import pytest

from freelance_bot.models import AiAssessment, Project
from freelance_bot.vk import (
    COMMAND_CLEAR_CHAT,
    COMMAND_FL,
    COMMAND_KWORK,
    COMMAND_RECENT,
    COMMAND_RESPONDED,
    COMMAND_SETTINGS,
    COMMAND_WRITE_RESPONSE,
    VkApiError,
    VkBot,
    format_message,
    keyboard_json,
    parse_command,
    project_keyboard_json,
    recent_keyboard_json,
    settings_keyboard_json,
    statistics_keyboard_json,
)


def test_keyboard_has_both_sources() -> None:
    keyboard = json.loads(keyboard_json())
    assert keyboard["inline"] is False
    assert keyboard["buttons"][0][0]["action"]["label"] == "🕘 Последние проекты"
    assert keyboard["buttons"][1][0]["action"]["label"] == "⚙ Настройки"
    assert keyboard["buttons"][2][0]["action"]["label"] == "📨 Отклики"
    assert keyboard["buttons"][1][0]["action"]["type"] == "callback"


def test_parse_button_payload_and_text() -> None:
    assert parse_command({"payload": '{"command":"last_kwork"}'}).name == COMMAND_KWORK
    assert parse_command({"text": "Последние 5 FL.ru"}).name == COMMAND_FL
    assert parse_command({"text": "Последние 10 FL.ru"}).name == COMMAND_FL
    assert parse_command({"text": "Настройки"}).name == COMMAND_SETTINGS
    assert parse_command({"text": "Последние проекты"}).name == COMMAND_RECENT
    assert parse_command({"text": "неизвестная команда"}) is None


def test_parse_callback_event_context() -> None:
    event = parse_command(
        {
            "payload": {"command": "settings"},
            "peer_id": 123,
            "conversation_message_id": 45,
            "event_id": "event-1",
        }
    )
    assert event is not None
    assert event.name == COMMAND_SETTINGS
    assert event.peer_id == 123
    assert event.conversation_message_id == 45
    assert event.event_id == "event-1"


def test_parse_project_action() -> None:
    keyboard = json.loads(project_keyboard_json("Kwork:123"))
    payload = keyboard["buttons"][0][0]["action"]["payload"]
    event = parse_command({"payload": payload})
    assert event is not None
    assert event.name == COMMAND_RESPONDED
    assert event.project_key == "Kwork:123"

    response_payload = keyboard["buttons"][1][0]["action"]["payload"]
    response_event = parse_command({"payload": response_payload})
    assert response_event is not None
    assert response_event.name == COMMAND_WRITE_RESPONSE
    assert response_event.project_key == "Kwork:123"


def test_project_decision_changes_button_state() -> None:
    neutral = json.loads(project_keyboard_json("Kwork:123"))["buttons"][0]
    assert neutral[0]["action"]["label"] == "Откликнулся"
    assert neutral[0]["color"] == "secondary"
    assert neutral[1]["action"]["label"] == "Не подошло"
    assert neutral[1]["color"] == "secondary"

    responded = json.loads(project_keyboard_json("Kwork:123", "responded"))["buttons"][0]
    assert responded[0]["action"]["label"] == "✅ Откликнулся"
    assert responded[0]["color"] == "positive"

    rejected = json.loads(project_keyboard_json("Kwork:123", "rejected"))["buttons"][0]
    assert rejected[1]["action"]["label"] == "❌ Не подошло"
    assert rejected[1]["color"] == "negative"

    write_response = json.loads(project_keyboard_json("Kwork:123"))["buttons"][1][0]
    assert write_response["action"]["label"] == "✍ Написать отклик"
    assert write_response["color"] == "primary"


def test_project_message_uses_compact_format_and_display_timezone() -> None:
    project = Project(
        "Kwork",
        "1",
        "Макет",
        "Описание",
        "до 500 ₽",
        "https://x",
        "Дизайн",
        datetime(2026, 7, 17, 12, 30, tzinfo=timezone.utc),
    )
    message = format_message(project, test_view=True)
    assert message == ("Kwork.ru\n\nМакет\n💰 до 500 ₽\n🕒 17.07.2026 17:30\n\nhttps://x")
    assert "Описание" not in message


def test_project_message_includes_ai_assessment_but_not_response() -> None:
    project = Project(
        "Kwork",
        "1",
        "Макет",
        "Описание",
        "до 500 ₽",
        "https://x",
        "Дизайн",
        datetime(2026, 7, 17, 12, 30, tzinfo=timezone.utc),
    )
    assessment = AiAssessment(
        project_key=project.key,
        suitable=True,
        score=91,
        reason="Задача соответствует профилю",
        response_text="Здравствуйте! Готов обсудить дизайн макета.",
        filter_model="GigaChat-2",
        response_model="GigaChat-2-Pro",
    )

    message = format_message(project, assessment=assessment)

    assert "🤖 Оценка: 91/100" in message
    assert "Почему: Задача соответствует профилю" in message
    assert "Готовый отклик" not in message
    assert assessment.response_text not in message


def test_recent_keyboard_requests_five_projects() -> None:
    labels = [
        button["action"]["label"]
        for row in json.loads(recent_keyboard_json())["buttons"]
        for button in row
    ]
    assert "5 подходящих Kwork" in labels
    assert "5 подходящих FL.ru" in labels
    assert "5 подходящих Profi.ru" in labels
    assert not any("Последние 10" in label for label in labels)


@pytest.mark.asyncio
async def test_project_does_not_send_ready_response_automatically() -> None:
    project = Project("Kwork", "1", "Макет", "Описание", "", "https://x", "Дизайн")
    assessment = AiAssessment(
        project_key=project.key,
        suitable=True,
        score=91,
        reason="Подходит",
        response_text="Готовый текст отклика",
        filter_model="GigaChat-2",
        response_model="GigaChat-2-Pro",
    )
    bot = object.__new__(VkBot)
    calls: list[tuple[str, str | bool]] = []

    async def fake_send_text(message: str, *, keyboard: str | bool = True) -> bool:
        calls.append((message, keyboard))
        return True

    bot.send_text = fake_send_text
    await bot.send(project, assessment=assessment)

    assert len(calls) == 1
    assert "Готовый текст отклика" not in calls[0][0]


def test_settings_keyboard_reflects_notification_state() -> None:
    keyboard = json.loads(
        settings_keyboard_json(kwork_enabled=True, fl_enabled=False, profi_enabled=True)
    )
    assert keyboard["buttons"][0][0]["action"]["label"] == "Kwork: ✅ вкл"
    assert keyboard["buttons"][1][0]["action"]["label"] == "FL.ru: ⛔ выкл"
    assert keyboard["buttons"][2][0]["action"]["label"] == "Profi.ru: ✅ вкл"
    clear_chat = keyboard["buttons"][3][0]
    assert clear_chat["action"]["label"] == "🗑 Очистить чат"
    assert clear_chat["color"] == "negative"
    event = parse_command({"payload": clear_chat["action"]["payload"]})
    assert event is not None
    assert event.name == COMMAND_CLEAR_CHAT


def test_statistics_keyboard_has_destructive_reset() -> None:
    keyboard = json.loads(statistics_keyboard_json())
    reset = keyboard["buttons"][0][0]
    assert reset["action"]["label"] == "🗑 Очистить статистику"
    assert reset["color"] == "negative"
    assert json.loads(reset["action"]["payload"])["command"] == "clear_statistics"


@pytest.mark.asyncio
async def test_keyboard_error_retries_without_keyboard() -> None:
    bot = object.__new__(VkBot)
    bot._user_id = 1
    calls: list[dict[str, object]] = []

    async def fake_api(method: str, **params: object) -> None:
        calls.append(params)
        if "keyboard" in params:
            raise VkApiError(912, "Chat bot feature is disabled")

    bot._api = fake_api

    assert await bot.send_text("test") is False
    assert len(calls) == 2
    assert "keyboard" in calls[0]
    assert "keyboard" not in calls[1]


@pytest.mark.asyncio
async def test_persistent_keyboard_service_message_is_deleted() -> None:
    bot = object.__new__(VkBot)
    bot._user_id = 1
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_api(method: str, **params: object) -> int | None:
        calls.append((method, params))
        return 77 if method == "messages.send" else None

    bot._api = fake_api
    await bot.set_persistent_keyboard('{"buttons":[]}')

    assert calls[0][0] == "messages.send"
    assert calls[0][1]["message"] == "\u2063"
    assert calls[1] == (
        "messages.delete",
        {"message_ids": 77, "delete_for_all": 1},
    )


@pytest.mark.asyncio
async def test_clear_chat_deletes_only_outgoing_messages_across_pages() -> None:
    bot = object.__new__(VkBot)
    bot._user_id = 1
    bot._ui_message_id = 99
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_api(method: str, **params: object) -> object:
        calls.append((method, params))
        if method == "messages.getHistory":
            if params["offset"] == 0:
                return {
                    "count": 3,
                    "items": [{"id": 10, "out": 1}, {"id": 11, "out": 0}],
                }
            return {"count": 3, "items": [{"id": 12, "out": 1}]}
        message_id = str(params["message_ids"])
        return {message_id: 1}

    bot._api = fake_api
    deleted, failed = await bot.clear_outgoing_messages(page_size=2, batch_size=1)

    assert (deleted, failed) == (2, 0)
    assert bot._ui_message_id is None
    delete_calls = [params for method, params in calls if method == "messages.delete"]
    assert [params["message_ids"] for params in delete_calls] == ["10", "12"]
    assert all(params["delete_for_all"] == 1 for params in delete_calls)
