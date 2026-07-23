import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from freelance_bot.models import Project

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://www.fl.ru"
MOSCOW_TZ = timezone(timedelta(hours=3))
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

# Это именно выбранные на скриншоте подкатегории, а не ключевые слова.
FL_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Сайты / Дизайн сайтов", "/projects/category/saity/web-dizajner-razrabotka-sajtov/"),
    ("Сайты / Тильда", "/projects/category/saity/tilda/"),
    ("Сайты / Редизайн сайтов", "/projects/category/saity/redizain-saitov/"),
    ("Сайты / Лендинги", "/projects/category/saity/landing/"),
    ("Дизайн / Дизайн сайтов", "/projects/category/dizajn/web-dizajner-verstalschik-dizajn/"),
    ("Дизайн / Интерфейсы", "/projects/category/dizajn/dizajner-interfejsov/"),
    ("Дизайн / Лэндинги", "/projects/category/dizajn/dizajn-lendingov/"),
    ("Дизайн / Мобильные приложения", "/projects/category/dizajn/dizayn-interfeysov-prilojeniy/"),
    ("Дизайн / UI/UX дизайн", "/projects/category/dizajn/ui-ux-dizajn/"),
    ("Дизайн / Редизайн сайтов", "/projects/category/dizajn/redeziain-saitov/"),
    ("Дизайн / Figma", "/projects/category/dizajn/figma/"),
)


def _text(node: object | None) -> str:
    if node is None or not hasattr(node, "get_text"):
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def parse_published_at(text: str, *, now: datetime | None = None) -> datetime | None:
    match = re.search(r"(\d{1,2})\s+([а-яё]+),\s*(\d{1,2}):(\d{2})", text.casefold())
    if match is None or match.group(2) not in RUSSIAN_MONTHS:
        return None
    current = (now or datetime.now(timezone.utc)).astimezone(MOSCOW_TZ)
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
    return published.astimezone(timezone.utc)


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


class FlSource:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _fetch_category(self, category: str, path: str) -> list[Project]:
        url = urljoin(BASE_URL, path)
        async with self._session.get(url) as response:
            response.raise_for_status()
            html = await response.text()
        projects = parse_projects(html, category)
        LOGGER.info("FL.ru: %s — найдено %d карточек", category, len(projects))
        return projects

    async def fetch(self) -> list[Project]:
        batches = await asyncio.gather(
            *(self._fetch_category(name, path) for name, path in FL_CATEGORIES),
            return_exceptions=True,
        )
        unique: dict[str, Project] = {}
        for (category, _), batch in zip(FL_CATEGORIES, batches, strict=True):
            if isinstance(batch, BaseException):
                LOGGER.error("FL.ru: ошибка рубрики %s: %s", category, batch)
                continue
            for project in batch:
                previous = unique.get(project.key)
                if previous is None:
                    unique[project.key] = project
                elif project.category not in previous.category:
                    unique[project.key] = Project(
                        source=previous.source,
                        external_id=previous.external_id,
                        title=previous.title,
                        description=previous.description,
                        price=previous.price,
                        url=previous.url,
                        category=f"{previous.category}; {project.category}",
                        published_at=previous.published_at,
                    )
        return list(unique.values())
