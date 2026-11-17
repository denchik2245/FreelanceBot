import asyncio
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from freelance_bot.main import _digest_due, _listen_for_commands
from freelance_bot.models import AiAssessment, Project
from freelance_bot.response_log import format_digest, format_panel, key_from_url
from freelance_bot.storage import ProjectStore
from freelance_bot.tracker import DIGEST_STATE, apply_file, digest_week, export_entries
from freelance_bot.tracker import main as tracker_main
from freelance_bot.vk import DISPLAY_TZ, BotCommand

NOW = datetime(2026, 11, 22, 15, 0, tzinfo=UTC)  # Sunday, 20:00 MSK+2


def project(source: str, number: int, *, offers: int | None = 30, hours_ago: float = 2,
            price: str = "до 30000 ₽", hired: float | None = 80) -> Project:
    url = (
        f"https://kwork.ru/projects/{number}/view"
        if source == "Kwork"
        else f"https://www.fl.ru/projects/{number}/sait.html"
    )
    return Project(
        source, str(number), f"Проект {number}", "Описание", price, url, "Дизайн",
        published_at=NOW - timedelta(hours=hours_ago),
        offers_count=offers, client_projects_count=12 if hired is not None else None,
        client_hired_percent=hired,
    )


def assessment(item: Project, clear: bool = True) -> AiAssessment:
    return AiAssessment(item.key, True, 100, "ok", "", "m", "", decision="accept",
                        requirements_clear=clear)


def remember(store: ProjectStore, item: Project) -> None:
    store.remember_project(item)
    store.remember_ai_assessment(assessment(item))


def test_button_records_snapshot_and_undo(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    item = project("Kwork", 1, offers=25, hours_ago=0.5)
    remember(store, item)
    store.responses.record(item, store.get_ai_assessment(item.key), now=NOW)
    entry = store.responses.get(item.key)
    assert entry is not None
    assert entry.level == "А"
    assert entry.offers_at_notice == 25
    assert entry.minutes_after_publication == 30
    assert entry.origin == "button"
    # A second press of the same card does not duplicate the entry.
    store.responses.record(item, None, now=NOW + timedelta(minutes=5))
    assert len(store.responses.entries(include_legacy=True)) == 1
    assert store.responses.discard(item.key)
    assert store.responses.get(item.key) is None
    store.close()


def test_reviewed_entry_survives_undo(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    item = project("FL.ru", 7)
    remember(store, item)
    store.responses.record(item, None, now=NOW)
    apply_file(store, [{"url": item.url, "viewed": True}])
    assert not store.responses.discard(item.key)
    assert store.responses.get(item.key).viewed is True
    store.close()


def test_review_updates_by_url_and_creates_missing(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    pressed = project("Kwork", 10)
    remember(store, pressed)
    store.responses.record(pressed, None, now=NOW - timedelta(days=1))
    known_but_not_pressed = project("FL.ru", 11)
    remember(store, known_but_not_pressed)
    result = apply_file(store, {"entries": [
        {"url": "https://kwork.ru/projects/10/view", "my_price": "25 000 ₽", "my_days": 9,
         "concept": True, "offers_at_response": 44, "outcome": "ordered", "order_amount": 25000},
        {"url": known_but_not_pressed.url, "viewed": False, "my_price": 20000},
        {"source": "Kwork", "external_id": "999", "title": "Не нажал кнопку",
         "url": "https://kwork.ru/projects/999/view", "responded_at": "2026-11-20 10:00:00",
         "viewed": True, "replied": True},
    ]})
    assert result == {"updated": 1, "created": 2}
    ordered = store.responses.get(pressed.key)
    assert ordered.outcome == "ordered" and ordered.viewed and ordered.replied
    assert ordered.my_price == 25000 and ordered.concept is True and ordered.offers == 44
    from_catalog = store.responses.get(known_but_not_pressed.key)
    assert from_catalog.origin == "review" and from_catalog.level is not None
    unknown = store.responses.get("Kwork:999")
    assert unknown.title == "Не нажал кнопку" and unknown.replied_at is not None
    assert store.responses.last_review() is not None
    store.close()


@pytest.mark.parametrize("bad", [
    {"url": "https://kwork.ru/projects/1/view", "outcome": "won"},
    {"url": "https://kwork.ru/projects/1/view", "viewed": "yes"},
    {"url": "https://kwork.ru/projects/1/view", "my_price": "-"},
    {"title": "без ссылки"},
])
def test_review_rejects_invalid_values(tmp_path: Path, bad: dict) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    with pytest.raises(ValueError):
        apply_file(store, [bad])
    store.close()


def test_republished_kwork_project_matches_by_canonical_key(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    item = Project("Kwork", "5", "Перезапуск", "", "до 10000 ₽",
                   "https://kwork.ru/projects/5/view", "Дизайн", published_at=NOW,
                   publication_id="1763800000")
    remember(store, item)
    store.responses.record(item, None, now=NOW)
    apply_file(store, [{"url": "https://kwork.ru/projects/5/view", "viewed": True}])
    assert store.responses.get(item.key).viewed is True
    assert len(store.responses.entries(include_legacy=True)) == 1
    store.close()


def test_digest_groups_by_level_concept_speed_and_crowd(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    fast = project("Kwork", 1, offers=20, hours_ago=24 * 2)
    slow = project("FL.ru", 2, offers=150, hours_ago=24 * 3, price="", hired=None)
    for item, delay in ((fast, 20), (slow, 300)):
        remember(store, item)
        store.responses.record(item, store.get_ai_assessment(item.key),
                               now=item.published_at + timedelta(minutes=delay))
    apply_file(store, [
        {"url": fast.url, "concept": True, "outcome": "ordered", "order_amount": 30000},
        {"url": slow.url, "concept": False, "viewed": False},
    ])
    text = format_digest(store.responses, now=NOW, display_tz=DISPLAY_TZ)
    assert "Откликов 2 (Kwork 1, FL.ru 1)" in text
    assert "Прочитано 1 из 2 (50%)" in text
    assert "заказов 1 на 30 000 ₽" in text
    assert "с концепцией: 1 → прочитано 1 из 1, написали 1, заказов 1" in text
    assert "до 1 часа: 1" in text and "позже часа: 1" in text
    assert "больше 120: 1" in text
    assert "Не сверено" not in text
    panel = format_panel(store.responses, now=NOW)
    assert "Откликнулся — 2" in panel and "Последний разбор" in panel
    store.close()


def test_digest_reminds_about_hanging_dialogs(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    item = project("Kwork", 3, hours_ago=24 * 6)
    remember(store, item)
    store.responses.record(item, None, now=NOW - timedelta(days=6))
    store.responses.apply([{"url": item.url, "replied": True}], now=NOW - timedelta(days=5))
    text = format_digest(store.responses, now=NOW, display_tz=DISPLAY_TZ)
    assert "Напомнить о себе" in text and "5 дн." in text
    store.close()


def test_legacy_presses_are_seeded_once(tmp_path: Path) -> None:
    path = tmp_path / "bot.sqlite3"
    store = ProjectStore(path)
    item = project("Kwork", 4)
    remember(store, item)
    store.set_project_decision(item.key, "responded")
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE response_log")
        connection.execute("DELETE FROM state WHERE name = 'tracker:seeded'")
    store = ProjectStore(path)
    entry = store.responses.get(item.key)
    assert entry is not None and entry.origin == "legacy"
    # Legacy entries are shown for review but not mixed into weekly numbers.
    assert store.responses.entries() == []
    apply_file(store, [{"url": item.url, "viewed": True}])
    assert [entry.project_key for entry in store.responses.entries()] == [item.key]
    store._connection.execute("UPDATE response_log SET reviewed_at = NULL")
    store.responses.discard(item.key)
    store.close()
    store = ProjectStore(path)
    assert store.responses.get(item.key) is None
    store.close()


def test_export_includes_open_older_entries(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    old = project("Kwork", 5)
    remember(store, old)
    store.responses.record(old, None, now=NOW - timedelta(days=20))
    data = export_entries(store, days=10, now=NOW)
    assert [entry["project_key"] for entry in data["entries"]] == [old.key]
    apply_file(store, [{"url": old.url, "outcome": "chose_other"}])
    assert export_entries(store, days=10, now=NOW)["entries"] == []
    store.close()


def test_cli_roundtrip(tmp_path: Path, monkeypatch, capsys) -> None:
    db = tmp_path / "bot.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    store = ProjectStore(db)
    item = project("Kwork", 6)
    remember(store, item)
    store.responses.record(item, None)
    store.close()
    assert tracker_main(["export"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["entries"][0]["url"] == item.url
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([{"url": item.url, "viewed": True}])))
    assert tracker_main(["apply", "-"]) == 0
    assert "Обновлено: 1" in capsys.readouterr().out
    assert tracker_main(["digest"]) == 0
    assert "Прочитано 1 из 1" in capsys.readouterr().out


def test_digest_schedule() -> None:
    class Settings:
        weekly_digest_hour = 20

    class Store:
        state: str | None = None

        def get_state(self, name):
            assert name == DIGEST_STATE
            return self.state

    store = Store()
    assert _digest_due(store, Settings(), NOW)
    assert not _digest_due(store, Settings(), NOW - timedelta(hours=1))
    assert not _digest_due(store, Settings(), NOW - timedelta(days=1))
    store.state = digest_week(NOW)
    assert not _digest_due(store, Settings(), NOW)
    Settings.weekly_digest_hour = -1
    store.state = None
    assert not _digest_due(store, Settings(), NOW)


def test_url_keys() -> None:
    assert key_from_url("https://kwork.ru/projects/3273787/view") == "Kwork:3273787"
    assert key_from_url("https://www.fl.ru/projects/5529530/nujen-sait.html") == "FL.ru:5529530"
    assert key_from_url("https://example.com") is None


async def test_responded_button_writes_tracker(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "bot.sqlite3")
    item = project("Kwork", 8)
    remember(store, item)
    commands = [
        BotCommand("responded", project_key=item.key),
        BotCommand("responses"),
        BotCommand("change_response_counter", source="Kwork", outcome="ordered", delta=1),
        BotCommand("weekly_digest"),
        BotCommand("responded", project_key=item.key),
    ]
    seen_after_first: list[bool] = []

    class Bot:
        def __init__(self):
            self.messages: list[str] = []

        async def set_persistent_keyboard(self, value):
            pass

        async def replace_ui(self, value):
            self.messages.append(value)

        async def listen(self, handler):
            for index, command in enumerate(commands):
                await handler(command)
                if index == 0:
                    seen_after_first.append(store.responses.get(item.key) is not None)

    bot = Bot()
    await _listen_for_commands(bot, store, None, None, None, asyncio.Lock(), asyncio.Lock(),
                               asyncio.Lock(), None, None, None)
    assert seen_after_first == [True]
    assert store.responses.get(item.key) is None
    assert bot.messages[0].startswith("📨 Отклики")
    assert bot.messages[1].startswith("📨 Отклики")
    assert bot.messages[2].startswith("📈 Отклики")
    store.close()
