import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_ai import _advisor

from freelance_bot.main import _listen_for_commands
from freelance_bot.models import AiAssessment, Project
from freelance_bot.project_filters import ProjectFilterManager
from freelance_bot.project_rating import rate_project
from freelance_bot.sources.fl import parse_projects
from freelance_bot.sources.kwork import KworkSource
from freelance_bot.storage import ProjectStore
from freelance_bot.vk import (
    BotCommand,
    config_texts_keyboard_json,
    format_message,
    project_keyboard_json,
)

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
PROJECT = Project(
    "Kwork", "42", "Дизайн лендинга", "Нужен макет 7 блоков для школы",
    "до 15 000 ₽", "https://kwork.ru/projects/42/view", "Дизайн", NOW,
    offers_count=50, client_projects_count=20, client_hired_percent=50,
)
ASSESSMENT = AiAssessment(
    PROJECT.key, True, 100, "Дизайн лендинга", "", "test", "",
    summary="Макет лендинга школы из 7 блоков", decision="accept", requirements_clear=True,
)


@pytest.mark.parametrize("changes,clear,expected", [
    ({}, True, "А"),
    ({"client_hired_percent": 49, "offers_count": 39}, True, "А"),
    ({"client_hired_percent": 49, "offers_count": 40}, True, "Б"),
    ({"client_hired_percent": 49, "offers_count": 120}, True, "Б"),
    ({"price": "14 999 ₽"}, True, "Б"),
    ({"price": "3 000 ₽"}, True, "Б"),
    ({"price": "2 999 ₽"}, True, "В"),
    ({"price": "0 ₽"}, True, "В"),
    ({"client_hired_percent": 0, "client_projects_count": 5}, True, "В"),
    ({"client_hired_percent": 0, "client_projects_count": 4}, True, "Б"),
    ({"client_hired_percent": 29, "client_projects_count": 10}, True, "В"),
    ({"client_hired_percent": 30, "client_projects_count": 10}, True, "Б"),
    ({"client_hired_percent": None, "client_projects_count": 0, "offers_count": 149}, True, "Б"),
    ({"client_hired_percent": None, "client_projects_count": 0, "offers_count": 150}, True, "В"),
    ({"client_hired_percent": None, "client_projects_count": None, "offers_count": 150},
     True, "Б"),
    ({"price": "30 000 ₽", "client_projects_count": 0, "client_hired_percent": 0,
      "offers_count": 168}, True, "А"),
    ({"price": "30 000 ₽", "client_projects_count": 10, "client_hired_percent": 0,
      "offers_count": 168}, True, "В"),
    ({"published_at": NOW - timedelta(days=14)}, True, "А"),
    ({"published_at": NOW - timedelta(days=14, seconds=1)}, True, "В"),
    ({}, False, "Б"),
    ({}, None, "Б"),
    ({"price": "по договоренности"}, True, "Б"),
    ({"price": "$100"}, True, "Б"),
    ({"price": "15 000 ₽/час"}, True, "Б"),
    ({"price": "15\u202f000 ₽"}, True, "А"),
    ({"client_hired_percent": None, "offers_count": None}, True, "Б"),
])
def test_rating_thresholds_and_conversion_plan_exception(changes, clear, expected):
    project = replace(PROJECT, **changes)
    assessment = replace(ASSESSMENT, requirements_clear=clear)
    assert rate_project(project, assessment, now=NOW).level == expected


def test_missing_metrics_are_unknown_instead_of_zero():
    project = replace(PROJECT, offers_count=None, client_hired_percent=None)
    rating = rate_project(project, ASSESSMENT, now=NOW)
    assert rating.level == "Б"
    assert "неизвестны найм и число откликов" in rating.reason
    assert rate_project(PROJECT, now=NOW).level == "Б"


@pytest.mark.parametrize("clarity", [True, False, None, "true", 1])
async def test_clarity_is_advisory_and_requires_a_real_boolean(clarity):
    advisor, client, _ = _advisor(85)
    client.responses = [json.dumps({
        "decision": "accept", "reason": "Веб-дизайн", "summary": "Макет лендинга",
        "requirements_clear": clarity,
    })]
    assessment = await advisor.assess(PROJECT)
    assert assessment.decision == "accept"
    expected = clarity if type(clarity) is bool else None
    assert assessment.requirements_clear is expected
    assert len(client.requests) == 1
    assert not hasattr(advisor, "generate_response")


@pytest.mark.parametrize("clear", [True, False, None])
def test_metadata_and_clarity_survive_upserts_and_reopening(tmp_path, clear):
    path = tmp_path / "bot.sqlite3"
    store = ProjectStore(path)
    store.remember_project(replace(PROJECT, offers_count=0))
    store.remember_project(PROJECT)
    store.remember_ai_assessment(replace(ASSESSMENT, requirements_clear=clear))
    store.close()
    store = ProjectStore(path)
    try:
        assert store.get_project(PROJECT.key) == PROJECT
        assert store.get_ai_assessment(PROJECT.key).requirements_clear is clear
        # A low advisory grade is never a new notification filter.
        cheap = replace(PROJECT, price="2 000 ₽")
        assert rate_project(cheap, ASSESSMENT, now=NOW).level == "В"
        assert ProjectFilterManager(store, default_ai_score=70).rejection_reason(cheap) is None
    finally:
        store.close()


def test_old_catalog_migrates_without_fabricating_hiring_history(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TABLE project_catalog (
                project_key TEXT PRIMARY KEY, source TEXT, external_id TEXT, title TEXT,
                description TEXT, price TEXT, url TEXT, category TEXT,
                published_at TEXT, last_seen_at TEXT
            )
        """)
        connection.execute("""
            INSERT INTO project_catalog VALUES
            ('Kwork:42','Kwork','42','Сайт','ТЗ','15 000 ₽','https://x','Сайты',NULL,NULL)
        """)
    store = ProjectStore(path)
    try:
        old = store.get_project("Kwork:42")
        assert old.offers_count is old.client_projects_count is old.client_hired_percent is None
        assert old.title == "Сайт"
    finally:
        store.close()


async def test_kwork_reads_offer_and_hiring_statistics_without_extra_requests():
    class Client:
        async def get_categories(self):
            return []

        async def get_projects(self, *, categories_ids, page):
            if page > 1:
                return []
            return [
                {"id": 1, "offers": 0, "user_projects_count": 5, "user_hired_percent": 0},
                {"id": 2, "offers": "39", "user_projects_count": "10",
                 "user_hired_percent": "50"},
                {"id": 3, "offers": -1, "user_projects_count": True,
                 "user_hired_percent": 101},
                SimpleNamespace(id=4, offers=70, user_projects_count=20, user_hired_percent=75),
            ]
    source = KworkSource.__new__(KworkSource)
    source._client = Client()
    source._category_labels = None
    first, second, invalid, model = await source.fetch()
    assert (first.offers_count, first.client_projects_count, first.client_hired_percent) == (0, 5, 0)
    assert (second.offers_count, second.client_hired_percent) == (39, 50)
    assert invalid.offers_count is invalid.client_projects_count is invalid.client_hired_percent is None
    assert (model.offers_count, model.client_hired_percent) == (70, 75)


def test_fl_offers_are_read_from_the_footer_without_hiring_guesses():
    from test_fl_parser import HTML

    html = HTML.replace('17 июля, 14:25</span>',
                        '17 июля, 14:25</span><a><span>39</span> ответов</a>')
    project = parse_projects(html, "Сайты")[0]
    assert project.offers_count == 39
    assert project.client_hired_percent is None
    assert parse_projects(HTML, "Сайты")[0].offers_count is None


def test_message_adds_a_grade_and_only_keeps_manual_response_tracking():
    current = replace(PROJECT, published_at=datetime.now(UTC))
    message = format_message(current, assessment=ASSESSMENT)
    assert "🏷 Уровень А" in message
    assert PROJECT.title in message and PROJECT.url in message
    assert ASSESSMENT.summary in message
    actions = json.loads(project_keyboard_json(PROJECT.key))["buttons"]
    assert [[b["action"]["label"] for b in row] for row in actions] == [["Откликнулся"]]
    assert "response" not in config_texts_keyboard_json()


@pytest.mark.parametrize("command", ["write_response", "rewrite_response"])
async def test_legacy_writer_buttons_cannot_call_ai(tmp_path, command):
    messages = []

    class Bot:
        async def set_persistent_keyboard(self, _value):
            pass

        async def send_text(self, message, **_kwargs):
            messages.append(message)

        async def listen(self, handler):
            await handler(BotCommand(command, project_key=PROJECT.key))

    store = ProjectStore(tmp_path / "bot.sqlite3")
    try:
        await _listen_for_commands(
            Bot(), store, None, None, None, None, None, None, object(), None, None,
        )
        assert messages == ["Генерация откликов удалена из бота."]
    finally:
        store.close()
