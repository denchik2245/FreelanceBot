from datetime import UTC, datetime

from freelance_bot.sources.kwork import KworkSource, build_category_labels


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[list[int | str], int]] = []
        self.timestamp = int(datetime.now(UTC).timestamp())

    async def get_categories(self) -> list[dict[str, object]]:
        return [
            {
                "id": 15,
                "name": "Дизайн",
                "subcategories": [{"id": 24, "name": "Веб и мобильный дизайн"}],
            }
        ]

    async def get_projects(
        self, *, categories_ids: list[int | str], page: int
    ) -> list[dict[str, object]]:
        self.calls.append((categories_ids, page))
        if page > 2:
            return []
        return [
            {
                "id": 122 + page,
                "category_id": 24,
                "title": "Дизайн приложения",
                "description": "Нарисовать интерфейс",
                "price": 10_000,
                "date_confirm": self.timestamp,
            }
        ]


async def test_fetch_uses_all_categories_and_multiple_pages() -> None:
    source = KworkSource.__new__(KworkSource)
    client = FakeClient()
    source._client = client
    source._category_labels = None

    projects = await source.fetch()

    assert client.calls == [(["all"], 1), (["all"], 2), (["all"], 3)]
    assert [project.external_id for project in projects] == ["123", "124"]
    assert projects[0].category == "Веб и мобильный дизайн"
    assert projects[0].url == "https://kwork.ru/projects/123/view"
    assert projects[0].published_at == datetime.fromtimestamp(client.timestamp, UTC)


async def test_manual_fetch_uses_targeted_categories_without_age_cutoff() -> None:
    source = KworkSource.__new__(KworkSource)
    client = FakeClient()
    source._client = client
    source._category_labels = None

    projects = await source.fetch_for_manual_selection()

    assert client.calls == [([24, 37], 1), ([24, 37], 2), ([24, 37], 3)]
    assert [project.external_id for project in projects] == ["123", "124"]


def test_build_category_labels() -> None:
    categories = [
        {
            "id": 15,
            "name": "Дизайн",
            "subcategories": [{"id": 24, "name": "Веб и мобильный дизайн"}],
        }
    ]

    assert build_category_labels(categories) == {
        15: "Дизайн",
        24: "Веб и мобильный дизайн",
    }
