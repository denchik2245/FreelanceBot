import logging
from datetime import datetime, timezone
from typing import Any

from freelance_bot.models import Project

LOGGER = logging.getLogger(__name__)
TARGET_CATEGORY_NAME = "Веб и мобильный дизайн"
TARGET_CATEGORY_LABEL = "Веб-дизайн / Мобильный дизайн"


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def resolve_target_category_id(categories: list[Any]) -> int:
    """Find Kwork's API category ID by its exact current name."""
    stack: list[Any] = list(categories)
    while stack:
        current = _plain(stack.pop())
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        if current.get("name") == TARGET_CATEGORY_NAME and current.get("id") is not None:
            return int(current["id"])
        for value in current.values():
            if isinstance(_plain(value), (dict, list)):
                stack.append(value)
    raise RuntimeError(f"Kwork не вернул рубрику «{TARGET_CATEGORY_NAME}»")


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
        self._target_category_id: int | None = None

    async def _get_target_category_id(self) -> int:
        if self._target_category_id is None:
            categories = await self._client.get_categories()
            self._target_category_id = resolve_target_category_id(categories)
            LOGGER.info(
                "Kwork: рубрика «%s» имеет category_id=%d",
                TARGET_CATEGORY_NAME,
                self._target_category_id,
            )
        return self._target_category_id

    async def fetch(self) -> list[Project]:
        category_id = await self._get_target_category_id()
        raw_projects = await self._client.get_projects(categories_ids=[category_id], page=1)
        projects: list[Project] = []
        for raw in raw_projects:
            project_id = _project_value(raw, "id")
            if project_id is None:
                continue
            # Защита от изменений/ошибок endpoint: не доверяем только параметру запроса.
            # Проект обязан сам сообщить тот же category_id.
            project_category_id = _project_value(raw, "category_id")
            if project_category_id is None or int(project_category_id) != category_id:
                LOGGER.warning(
                    "Kwork: проект %s отброшен, ожидался category_id=%d, получен %r",
                    project_id,
                    category_id,
                    project_category_id,
                )
                continue
            price = _project_value(raw, "price")
            date_confirm = _project_value(raw, "date_confirm")
            projects.append(
                Project(
                    source="Kwork",
                    external_id=str(project_id),
                    title=str(_project_value(raw, "title", "Без названия") or "Без названия"),
                    description=str(_project_value(raw, "description", "") or ""),
                    price=f"до {price} ₽" if price else "по договоренности",
                    url=f"https://kwork.ru/projects/{project_id}/view",
                    category=TARGET_CATEGORY_LABEL,
                    published_at=(
                        datetime.fromtimestamp(int(date_confirm), timezone.utc)
                        if date_confirm
                        else None
                    ),
                )
            )
        LOGGER.info("Kwork: найдено %d карточек", len(projects))
        return projects

    async def close(self) -> None:
        await self._client.close()
