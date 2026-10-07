import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from freelance_bot.main import _format_feedback_summary, _listen_for_commands
from freelance_bot.models import Project
from freelance_bot.storage import ProjectStore
from freelance_bot.vk import BotCommand, parse_command, responses_keyboard_json


def add_response(store: ProjectStore, source: str, number: int) -> Project:
    project = Project(source, str(number), f"Проект {number}", "", "", "https://x", "Дизайн")
    store.remember_project(project)
    store.set_project_decision(project.key, "responded")
    return project


def test_independent_outcomes_and_response_toggle(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    first = add_response(store, "Kwork", 1)
    second = add_response(store, "Kwork", 2)
    add_response(store, "FL.ru", 3)
    store.change_response_counter("Kwork", "client_replied")
    store.change_response_counter("Kwork", "ordered")
    store.change_response_counter("FL.ru", "client_refused")
    assert store.get_project_feedback(first.key) == ("responded", None)
    assert store.get_project_feedback(second.key) == ("responded", None)
    assert store.feedback_counts_by_source()["Kwork"]["responded"] == 2
    assert store.feedback_counts_by_source()["FL.ru"]["responded"] == 1
    # Removing an unrelated response does not remove a source's recorded results.
    assert store.toggle_project_decision(second.key, "responded") is None
    assert store.feedback_counts()["ordered"] == 1
    assert store.feedback_counts()["client_replied"] == 1
    with pytest.raises(ValueError, match="Сначала уменьшите"):
        store.toggle_project_decision(first.key, "responded")
    assert store.get_project_feedback(first.key) == ("responded", None)
    store.change_response_counter("Kwork", "ordered", -1)
    store.change_response_counter("Kwork", "client_replied", -1)
    assert store.toggle_project_decision(first.key, "responded") is None
    store.close()


def test_counter_bounds_terminal_results_and_deduplication(tmp_path: Path) -> None:
    path = tmp_path / "bot.sqlite3"
    store = ProjectStore(path)
    with pytest.raises(ValueError, match="больше"):
        store.change_response_counter("FL.ru", "client_replied")
    add_response(store, "FL.ru", 1)
    assert store.change_response_counter("FL.ru", "client_replied", event_id="reply-1")
    assert not store.change_response_counter("FL.ru", "client_replied", event_id="reply-1")
    store.change_response_counter("FL.ru", "ordered", event_id="order-1")
    # A conversation and its order both count, but terminal outcomes are exclusive.
    with pytest.raises(ValueError, match="больше"):
        store.change_response_counter("FL.ru", "client_refused")
    store.change_response_counter("FL.ru", "ordered", -1)
    store.change_response_counter("FL.ru", "client_chose_other")
    with pytest.raises(ValueError, match="нулю"):
        store.change_response_counter("FL.ru", "ordered", -1)
    with pytest.raises(ValueError, match="только для FL"):
        store.change_response_counter("Kwork", "client_refused")
    with pytest.raises(ValueError):
        store.change_response_counter("unknown", "ordered")
    with pytest.raises(ValueError):
        store.change_response_counter("FL.ru", "unknown")
    for invalid in (0, 2, True, "1"):
        with pytest.raises(ValueError):
            store.change_response_counter("FL.ru", "ordered", invalid)
    store.close()
    store = ProjectStore(path)
    assert store.feedback_counts()["client_replied"] == 1
    assert store.feedback_counts()["client_chose_other"] == 1
    assert not store.change_response_counter("FL.ru", "client_replied", event_id="reply-1")
    store.reset_feedback_statistics()
    assert not store.change_response_counter("FL.ru", "client_replied", event_id="reply-1")
    assert store.feedback_counts()["responded"] == 0
    assert store.feedback_counts()["client_chose_other"] == 0
    store.close()


def test_migration_preserves_only_counted_outcomes_once(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TABLE project_feedback (
                project_key TEXT PRIMARY KEY, decision TEXT NOT NULL, outcome TEXT,
                decision_at TEXT DEFAULT CURRENT_TIMESTAMP, outcome_at TEXT,
                stats_counted INTEGER DEFAULT 1, hidden_from_responses INTEGER DEFAULT 0
            )
        """)
        connection.executemany(
            "INSERT INTO project_feedback VALUES (?, ?, ?, CURRENT_TIMESTAMP, NULL, ?, ?)",
            [
                ("Kwork:1", "responded", "client_replied", 1, 1),
                ("Kwork:2", "responded", "client_chose_other", 1, 0),
                ("FL.ru:3", "responded", "client_replied", 1, 0),
                ("FL.ru:4", "responded", "client_replied", 0, 0),
                ("FL.ru:5", "rejected", None, 1, 0),
                ("Profi.ru:6", "responded", "client_replied", 1, 0),
            ],
        )
    store = ProjectStore(path)
    counts = store.feedback_counts_by_source()
    assert counts["Kwork"]["responded"] == 2
    assert counts["Kwork"]["client_replied"] == 1
    assert counts["Kwork"]["client_chose_other"] == 1
    assert counts["FL.ru"]["responded"] == 1
    assert counts["FL.ru"]["client_replied"] == 1
    assert counts["Profi.ru"]["client_replied"] == 1
    store.close()
    store = ProjectStore(path)
    assert store.feedback_counts_by_source() == counts
    store.reset_feedback_statistics()
    store.close()
    store = ProjectStore(path)
    assert store.feedback_counts()["responded"] == 0
    assert store.feedback_counts()["client_replied"] == 0
    assert store.get_project_feedback("Kwork:1") == ("responded", "client_replied")
    store.close()


def test_summary_uses_each_source_denominator_and_fl_refusals(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    for number in range(4):
        add_response(store, "Kwork", number)
    for number in range(2):
        add_response(store, "FL.ru", number)
    store.change_response_counter("Kwork", "client_replied")
    store.change_response_counter("Kwork", "ordered")
    store.change_response_counter("FL.ru", "client_refused")
    message = _format_feedback_summary(store)
    kwork_block = message.split("\nKwork\n")[1].split("\nFL.ru\n")[0]
    fl_block = message.split("\nFL.ru\n")[1]
    assert "Откликнулся → Написали мне — 25,0%" in kwork_block
    assert "Откликнулся → Заказали — 25,0%" in kwork_block
    assert "Отказали" not in kwork_block
    assert "Доля отказов — 50,0%" in fl_block
    assert "Проект" not in message
    assert "Profi.ru" not in message
    store.reset_feedback_statistics()
    assert "нет откликов" in _format_feedback_summary(store)
    store.close()


async def test_response_commands_update_summary_without_project_list(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    add_response(store, "Kwork", 1)
    add_response(store, "FL.ru", 2)
    keyboard = json.loads(responses_keyboard_json("FL.ru"))
    reply = parse_command(
        {
            "payload": keyboard["buttons"][2][0]["action"]["payload"],
            "event_id": "reply-1",
        }
    )
    order = parse_command(
        {
            "payload": keyboard["buttons"][4][0]["action"]["payload"],
            "event_id": "order-1",
        }
    )
    commands = [
        BotCommand("responses"),
        BotCommand("response_source", source="FL.ru"),
        reply,
        reply,
        order,
        BotCommand("client_chose_other", project_key="Kwork:1"),
        BotCommand("response_project", project_key="Kwork:1"),
        BotCommand("change_response_counter", source="Kwork", outcome="client_refused", delta=1),
    ]

    class Bot:
        def __init__(self):
            self.messages: list[str] = []
            self.keyboards: list[str] = []

        async def set_persistent_keyboard(self, value):
            self.keyboards.append(value)

        async def replace_ui(self, value):
            self.messages.append(value)

        async def listen(self, handler):
            for command in commands:
                await handler(command)

    bot = Bot()
    await _listen_for_commands(
        bot,
        store,
        None,
        None,
        None,
        asyncio.Lock(),
        asyncio.Lock(),
        asyncio.Lock(),
        None,
        None,
        None,
    )
    assert store.feedback_counts_by_source()["FL.ru"]["client_replied"] == 1
    assert store.feedback_counts_by_source()["FL.ru"]["ordered"] == 1
    assert store.feedback_counts_by_source()["Kwork"]["client_chose_other"] == 0
    assert all("Проект 1" not in message and "https://x" not in message for message in bot.messages)
    assert "Отказы учитываются только для FL.ru" in bot.messages[-1]
    assert "Кнопки сейчас меняют счетчики FL.ru" in bot.messages[2]
    store.close()
