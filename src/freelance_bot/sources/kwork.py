import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from freelance_bot.models import Project

LOGGER = logging.getLogger(__name__)
ALL_CATEGORIES_LABEL = "Все категории"
MAX_PAGES = 10
MAX_PROJECT_AGE = timedelta(hours=24)


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

    async def fetch(self) -> list[Project]:
        category_labels = await self._get_category_labels()
        unique: dict[str, Project] = {}
        cutoff = datetime.now(UTC) - MAX_PROJECT_AGE
        for page in range(1, MAX_PAGES + 1):
            raw_projects = await self._client.get_projects(categories_ids=["all"], page=page)
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
            LOGGER.info("Kwork: общая лента, страница %d — найдено %d карточек", page, len(page_projects))
            dates = [project.published_at for project in page_projects if project.published_at]
            if dates and max(dates) < cutoff:
                break
        LOGGER.info("Kwork: в общей ленте найдено %d уникальных карточек", len(unique))
        return list(unique.values())

    async def close(self) -> None:
        await self._client.close()
