import json
from datetime import datetime, timezone

import pytest

from freelance_bot.models import Project
from freelance_bot.vk import (
    COMMAND_FL,
    COMMAND_KWORK,
    COMMAND_SETTINGS,
    COMMAND_RESPONDED,
    COMMAND_RECENT,
    format_message,
    keyboard_json,
    parse_command,
    project_keyboard_json,
    settings_keyboard_json,
    statistics_keyboard_json,
    VkApiError,
    VkBot,
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
    assert message == (
        "Kwork.ru\n\nМакет\n💰 до 500 ₽\n"
        "🕒 17.07.2026 17:30\n\nhttps://x"
    )
    assert "Описание" not in message


def test_settings_keyboard_reflects_notification_state() -> None:
    keyboard = json.loads(
        settings_keyboard_json(kwork_enabled=True, fl_enabled=False, profi_enabled=True)
    )
    assert keyboard["buttons"][0][0]["action"]["label"] == "Kwork: ✅ вкл"
    assert keyboard["buttons"][1][0]["action"]["label"] == "FL.ru: ⛔ выкл"
    assert keyboard["buttons"][2][0]["action"]["label"] == "Profi.ru: ✅ вкл"


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
