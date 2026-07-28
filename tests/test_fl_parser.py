from datetime import UTC, datetime
from typing import Self

from freelance_bot.sources.fl import (
    ALL_PROJECTS_CATEGORY,
    FlSource,
    parse_projects,
    parse_published_at,
)

HTML = """
<div id="projects-list">
  <div id="project-item5514216" class="b-post">
    <div class="b-post__grid">
      <h2><a data-disposable-project-id="5514216"
        href="/projects/5514216/test.html">Дизайн приложения</a></h2>
      <div class="b-post__price"><span>50 000 руб</span></div>
      <div class="b-post__grid_descript"><div class="b-post__txt">Нужен UX/UI</div></div>
      <div class="b-post__foot"><span class="text-gray-opacity-4">17 июля, 14:25</span></div>
    </div>
  </div>
</div>
"""


def test_parse_projects() -> None:
    projects = parse_projects(HTML, "Дизайн / UI/UX дизайн")
    assert len(projects) == 1
    assert projects[0].external_id == "5514216"
    assert projects[0].title == "Дизайн приложения"
    assert projects[0].price == "50 000 руб"
    assert projects[0].url == "https://www.fl.ru/projects/5514216/test.html"
    assert projects[0].published_at == datetime(2026, 7, 17, 11, 25, tzinfo=UTC)


def test_parse_published_at_handles_previous_year() -> None:
    now = datetime(2026, 1, 2, 10, tzinfo=UTC)
    assert parse_published_at("31 декабря, 23:10", now=now) == datetime(
        2025, 12, 31, 20, 10, tzinfo=UTC
    )


def test_parse_published_at_handles_relative_time() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)

    assert parse_published_at("Заказ Только что", now=now) == now
    assert parse_published_at("Заказ 1 час 3 минуты назад", now=now) == datetime(
        2026, 7, 28, 10, 57, tzinfo=UTC
    )


class FakeResponse:
    def __init__(self, html: str) -> None:
        self._html = html

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def text(self) -> str:
        return self._html


class FakeSession:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.urls.append(url)
        page = len(self.urls)
        if page > 2:
            return FakeResponse("")
        return FakeResponse(
            HTML.replace("5514216", str(5514215 + page)).replace(
                "17 июля, 14:25", "Только что"
            )
        )


async def test_fetch_reads_paginated_all_projects_feed() -> None:
    session = FakeSession()
    source = FlSource(session)  # type: ignore[arg-type]

    projects = await source.fetch()

    assert session.urls == [
        "https://www.fl.ru/projects/?kind=1",
        "https://www.fl.ru/projects/page-2/?kind=1",
        "https://www.fl.ru/projects/page-3/?kind=1",
    ]
    assert [project.external_id for project in projects] == ["5514216", "5514217"]
    assert all(project.category == ALL_PROJECTS_CATEGORY for project in projects)
