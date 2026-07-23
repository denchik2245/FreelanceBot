from freelance_bot.sources.kwork import (
    TARGET_CATEGORY_LABEL,
    KworkSource,
    resolve_target_category_id,
)
from datetime import datetime, timezone


class FakeClient:
    def __init__(self) -> None:
        self.categories_ids: list[int | str] | None = None

    async def get_projects(
        self, *, categories_ids: list[int | str], page: int
    ) -> list[dict[str, object]]:
        self.categories_ids = categories_ids
        assert page == 1
        return [
            {
                "id": 123,
                "category_id": 24,
                "title": "Дизайн приложения",
                "description": "Нарисовать интерфейс",
                "price": 10_000,
                "date_confirm": 1_784_298_445,
            }
        ]


async def test_fetch_uses_account_favorite_categories() -> None:
    source = KworkSource.__new__(KworkSource)
    client = FakeClient()
    source._client = client
    source._target_category_id = 24

    projects = await source.fetch()

    assert client.categories_ids == [24]
    assert projects[0].category == TARGET_CATEGORY_LABEL
    assert projects[0].url == "https://kwork.ru/projects/123/view"
    assert projects[0].published_at == datetime.fromtimestamp(1_784_298_445, timezone.utc)


def test_resolve_target_category_id() -> None:
    categories = [
        {
            "id": 15,
            "name": "Дизайн",
            "subcategories": [
                {"id": 25, "name": "Логотип и брендинг"},
                {"id": 24, "name": "Веб и мобильный дизайн"},
            ],
        }
    ]
    assert resolve_target_category_id(categories) == 24


async def test_fetch_rejects_wrong_category_from_api() -> None:
    source = KworkSource.__new__(KworkSource)
    client = FakeClient()
    source._client = client
    source._target_category_id = 999

    projects = await source.fetch()

    assert projects == []
