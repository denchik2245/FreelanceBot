from datetime import datetime, timezone
from typing import Any

import pytest

from freelance_bot.sources.profi import (
    AUTH_QUERY,
    BASE_URL,
    ProfiSource,
    _is_auth_graphql_error,
    parse_projects,
)


def test_parse_projects() -> None:
    payload = {
        "data": {
            "boSearchBoardItems": {
                "items": [
                    {
                        "id": "987654",
                        "type": "SNIPPET",
                        "title": "Создать лендинг на Tilda",
                        "description": "Нужен дизайн и сборка",
                        "lastUpdateDate": "2026-07-17T15:20:00Z",
                        "price": {"prefix": "до", "value": "15 000", "suffix": "₽"},
                    },
                    {"id": "block", "type": "PREMIUM_BLOCK"},
                ]
            }
        }
    }

    projects = parse_projects(payload)

    assert len(projects) == 1
    assert projects[0].source == "Profi.ru"
    assert projects[0].title == "Создать лендинг на Tilda"
    assert projects[0].price == "до 15 000 ₽"
    assert projects[0].url == "https://profi.ru/backoffice/n.php?o=987654"
    assert projects[0].published_at == datetime(2026, 7, 17, 15, 20, tzinfo=timezone.utc)


def test_parse_projects_includes_carousel_and_deduplicates_ids() -> None:
    payload = {
        "data": {
            "boSearchBoardItems": {
                "items": [
                    {
                        "id": "123",
                        "type": "SNIPPET",
                        "title": "Сделать сайт",
                        "lastUpdateDate": "1784301600000",
                    },
                    {
                        "id": "carousel",
                        "type": "CAROUSEL",
                        "snippets": [
                            {
                                "id": "123",
                                "title": "Дубликат",
                                "lastUpdateDate": 1784301600,
                            },
                            {
                                "id": "456",
                                "title": "Дизайн лендинга",
                                "description": "Нужен макет в Figma",
                                "lastUpdateDate": 1784301600,
                                "price": {"value": 12000, "suffix": "₽"},
                            },
                        ],
                    },
                    {"id": "promo", "type": "PREMIUM_BLOCK", "title": "Реклама"},
                ]
            }
        }
    }

    projects = parse_projects(payload)

    assert [project.external_id for project in projects] == ["123", "456"]
    assert projects[0].published_at == datetime.fromtimestamp(1784301600, timezone.utc)
    assert projects[1].price == "12000 ₽"


def test_access_denied_is_treated_as_expired_authentication() -> None:
    assert _is_auth_graphql_error([{"message": "Access denied"}])


@pytest.mark.asyncio
async def test_fetch_projects_follows_cursor_and_deduplicates() -> None:
    payloads = [
        {
            "data": {
                "boSearchBoardItems": {
                    "nextCursor": "page-2",
                    "items": [
                        {"id": "1", "type": "SNIPPET", "title": "Первый"},
                    ],
                }
            }
        },
        {
            "data": {
                "boSearchBoardItems": {
                    "nextCursor": None,
                    "items": [
                        {"id": "1", "type": "SNIPPET", "title": "Дубликат"},
                        {"id": "2", "type": "SNIPPET", "title": "Второй"},
                    ],
                }
            }
        },
    ]
    cursors: list[str | None] = []
    source = ProfiSource.__new__(ProfiSource)

    async def graphql(_query: str, variables: dict[str, Any]) -> dict[str, Any]:
        cursors.append(variables["nextCursor"])
        return payloads.pop(0)

    source._authorized_graphql = graphql  # type: ignore[method-assign]

    projects = await source._fetch_projects(max_pages=5, cutoff=None, feed_label="тест")

    assert cursors == [None, "page-2"]
    assert [project.external_id for project in projects] == ["1", "2"]


@pytest.mark.asyncio
async def test_fetch_uses_standard_account_verticals() -> None:
    source = ProfiSource.__new__(ProfiSource)
    variables_seen: dict[str, Any] = {}

    class BrowserSession:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def graphql(_query: str, variables: dict[str, Any]) -> dict[str, Any]:
        variables_seen.update(variables)
        return {"data": {"boSearchBoardItems": {"nextCursor": None, "items": []}}}

    source._browser_session = lambda: BrowserSession()  # type: ignore[method-assign]
    source._authorized_graphql = graphql  # type: ignore[method-assign]

    await source.fetch()

    assert variables_seen["useSavedFilter"] is True
    assert variables_seen["allVerticals"] is False


@pytest.mark.asyncio
async def test_login_uses_current_webbo_auth_contract() -> None:
    class Page:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def goto(self, url: str, **_kwargs: object) -> None:
            self.urls.append(url)

    source = ProfiSource(object(), "login", "password")  # type: ignore[arg-type]
    page = Page()
    source._browser_page = page
    requests: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    async def graphql(
        query: str,
        variables: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        requests.append((query, variables, headers))
        if query == AUTH_QUERY:
            return {
                "data": {
                    "authStrategyStart": {
                        "result": {
                            "__typename": "AuthStrategyResultSuccess",
                            "auth": {"loginUrl": "/backoffice/login/success"},
                            "step": None,
                        }
                    }
                }
            }
        raise AssertionError("После успешного входа отдельный запрос профиля не нужен")

    source._graphql = graphql  # type: ignore[method-assign]

    await source._login_to_account()

    _, variables, headers = requests[0]
    assert variables["initialState"]["currentHost"] == BASE_URL
    assert variables["initialState"]["defaultOrderCityId"] == "prfr"
    assert variables["initialState"]["implementer"]["siteId"] == "1815"
    assert headers is not None
    assert headers["X-Auth-Flow-Uuid"] == source._auth_flow_id
    assert headers["x-warp-ui-app"] == "WEBBO"
    assert headers["x-warp-ui-ver"] == "100"
    assert page.urls == [f"{BASE_URL}/backoffice/login/success"]
    assert len(requests) == 1
    assert source._authenticated
