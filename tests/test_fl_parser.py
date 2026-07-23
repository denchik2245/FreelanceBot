from datetime import datetime, timezone

from freelance_bot.sources.fl import parse_projects, parse_published_at


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
    assert projects[0].published_at == datetime(2026, 7, 17, 11, 25, tzinfo=timezone.utc)


def test_parse_published_at_handles_previous_year() -> None:
    now = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
    assert parse_published_at("31 декабря, 23:10", now=now) == datetime(
        2025, 12, 31, 20, 10, tzinfo=timezone.utc
    )
