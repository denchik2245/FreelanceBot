import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from freelance_bot.models import Project

LOGGER = logging.getLogger(__name__)
ALL_CATEGORIES_LABEL = "Все категории"
MAX_PAGES = 10
MAX_PROJECT_AGE = timedelta(hours=24)
MANUAL_CATEGORY_IDS = [24, 37]  # Веб и мобильный дизайн; Создание сайта
MANUAL_MAX_PAGES = 10


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def build_category_labels(categories: list[Any]) -> dict[int, str]:
    """Build an ID-to-name lookup from Kwork's nested category tree."""
    labels: dict[int, str] = {}
    stack: list[Any] = list(categories)
    while stack:
        current = _plain(stack.pop())
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        if current.get("id") is not None and current.get("name"):
            labels[int(current["id"])] = str(current["name"])
        for value in current.values():
            if isinstance(_plain(value), (dict, list)):
                stack.append(value)
    return labels


def _project_value(project: Any, name: str, default: Any = None) -> Any:
    if hasattr(project, name):
        return getattr(project, name)
    if isinstance(project, dict):
        return project.get(name, default)
    return default


class KworkSource:
    def __init__(self, login: str, password: str) -> None:
        from kwork import Kwork

        self._client = Kwork(
            login=login,
            password=password,
            timeout=30.0,
            retry_max_attempts=3,
        )
        self._category_labels: dict[int, str] | None = None

    async def _get_category_labels(self) -> dict[int, str]:
        if self._category_labels is None:
            categories = await self._client.get_categories()
            self._category_labels = build_category_labels(categories)
            LOGGER.info("Kwork: загружено %d категорий", len(self._category_labels))
        return self._category_labels

    async def _fetch_projects(
        self,
        *,
        categories_ids: list[int | str],
        max_pages: int,
        cutoff: datetime | None,
        feed_label: str,
    ) -> list[Project]:
        category_labels = await self._get_category_labels()
        unique: dict[str, Project] = {}
        for page in range(1, max_pages + 1):
            try:
                raw_projects = await self._client.get_projects(
                    categories_ids=categories_ids,
                    page=page,
                )
            except KeyError:
                # Some Kwork API responses omit `response` after the last page.
                # Keep already collected projects, but do not mask a first-page failure.
                if not unique:
                    raise
                LOGGER.info("Kwork: %s закончилась на странице %d", feed_label, page)
                break
            if not raw_projects:
                break
            page_projects: list[Project] = []
            for raw in raw_projects:
                project_id = _project_value(raw, "id")
                if project_id is None:
                    continue
                category_id = _project_value(raw, "category_id")
                price = _project_value(raw, "price")
                date_confirm = _project_value(raw, "date_confirm")
                project = Project(
                    source="Kwork",
                    external_id=str(project_id),
                    title=str(_project_value(raw, "title", "Без названия") or "Без названия"),
                    description=str(_project_value(raw, "description", "") or ""),
                    price=f"до {price} ₽" if price else "по договоренности",
                    url=f"https://kwork.ru/projects/{project_id}/view",
                    category=(
                        category_labels.get(int(category_id), f"Категория Kwork #{category_id}")
                        if category_id is not None
                        else ALL_CATEGORIES_LABEL
                    ),
                    published_at=(
                        datetime.fromtimestamp(int(date_confirm), UTC)
                        if date_confirm
                        else None
                    ),
                )
                page_projects.append(project)
                unique.setdefault(project.key, project)
            LOGGER.info(
                "Kwork: %s, страница %d — найдено %d карточек",
                feed_label,
                page,
                len(page_projects),
            )
            dates = [project.published_at for project in page_projects if project.published_at]
            if cutoff is not None and dates and max(dates) < cutoff:
                break
        LOGGER.info("Kwork: %s — найдено %d уникальных карточек", feed_label, len(unique))
        return list(unique.values())

    async def fetch(self) -> list[Project]:
        return await self._fetch_projects(
            categories_ids=["all"],
            max_pages=MAX_PAGES,
            cutoff=datetime.now(UTC) - MAX_PROJECT_AGE,
            feed_label="общая лента",
        )

    async def fetch_for_manual_selection(self) -> list[Project]:
        """Fetch the deeper targeted feed used to assemble five suitable projects."""
        return await self._fetch_projects(
            categories_ids=MANUAL_CATEGORY_IDS,
            max_pages=MANUAL_MAX_PAGES,
            cutoff=None,
            feed_label="целевая выдача веб-дизайна и создания сайтов",
        )

    async def close(self) -> None:
        await self._client.close()
