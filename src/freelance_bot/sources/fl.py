import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from freelance_bot.models import Project

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://www.fl.ru"
MOSCOW_TZ = timezone(timedelta(hours=3))
ALL_PROJECTS_CATEGORY = "Все категории"
MAX_PAGES = 10
MAX_PROJECT_AGE = timedelta(hours=24)
RUSSIAN_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

def _text(node: object | None) -> str:
    if node is None or not hasattr(node, "get_text"):
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def parse_published_at(text: str, *, now: datetime | None = None) -> datetime | None:
    current = (now or datetime.now(UTC)).astimezone(MOSCOW_TZ)
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    if "только что" in normalized:
        return current.astimezone(UTC)
    if "назад" in normalized:
        hours_match = re.search(r"(\d+)\s+час", normalized)
        minutes_match = re.search(r"(\d+)\s+минут", normalized)
        if hours_match is not None or minutes_match is not None:
            published = current - timedelta(
                hours=int(hours_match.group(1)) if hours_match else 0,
                minutes=int(minutes_match.group(1)) if minutes_match else 0,
            )
            return published.astimezone(UTC)
    match = re.search(r"(\d{1,2})\s+([а-я]+),\s*(\d{1,2}):(\d{2})", normalized)
    if match is None or match.group(2) not in RUSSIAN_MONTHS:
        return None
    published = datetime(
        current.year,
        RUSSIAN_MONTHS[match.group(2)],
        int(match.group(1)),
        int(match.group(3)),
        int(match.group(4)),
        tzinfo=MOSCOW_TZ,
    )
    if published > current + timedelta(days=1):
        published = published.replace(year=published.year - 1)
    return published.astimezone(UTC)


def _card_published_at(card: object) -> datetime | None:
    if not hasattr(card, "select"):
        return None
    for node in card.select(".b-post__foot span"):
        parsed = parse_published_at(_text(node))
        if parsed is not None:
            return parsed
    return None


def parse_projects(html: str, category: str) -> list[Project]:
    soup = BeautifulSoup(html, "html.parser")
    projects: list[Project] = []
    for card in soup.select("#projects-list [id^='project-item']"):
        link = card.select_one("h2 a[data-disposable-project-id]")
        if link is None:
            continue
        external_id = str(link.get("data-disposable-project-id", "")).strip()
        href = str(link.get("href", "")).strip()
        if not external_id or not href:
            match = re.search(r"/projects/(\d+)/", href)
            external_id = match.group(1) if match else ""
        if not external_id:
            continue
        projects.append(
            Project(
                source="FL.ru",
                external_id=external_id,
                title=_text(link),
                description=_text(card.select_one(".b-post__grid_descript .b-post__txt")),
                price=_text(card.select_one(".b-post__price")),
                url=urljoin(BASE_URL, href),
                category=category,
                published_at=_card_published_at(card),
            )
        )
    return projects


def _page_path(page: int) -> str:
    path = "/projects/" if page == 1 else f"/projects/page-{page}/"
    return f"{path}?kind=1"


class FlSource:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _fetch_page(self, page: int) -> list[Project]:
        url = urljoin(BASE_URL, _page_path(page))
        async with self._session.get(url) as response:
            response.raise_for_status()
            html = await response.text()
        projects = parse_projects(html, ALL_PROJECTS_CATEGORY)
        LOGGER.info("FL.ru: общая лента, страница %d — найдено %d карточек", page, len(projects))
        return projects

    async def fetch(self) -> list[Project]:
        unique: dict[str, Project] = {}
        cutoff = datetime.now(UTC) - MAX_PROJECT_AGE
        for page in range(1, MAX_PAGES + 1):
            projects = await self._fetch_page(page)
            if not projects:
                break
            for project in projects:
                unique.setdefault(project.key, project)
            dates = [project.published_at for project in projects if project.published_at is not None]
            if dates and max(dates) < cutoff:
                break
        LOGGER.info("FL.ru: в общей ленте найдено %d уникальных карточек", len(unique))
        return list(unique.values())
