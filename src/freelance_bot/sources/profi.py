from datetime import datetime, timezone
import logging
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

import aiohttp

from freelance_bot.models import Project

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://profi.ru"
GRAPHQL_URL = f"{BASE_URL}/graphql"
TARGET_CATEGORY_LABEL = "Услуги аккаунта Profi.ru"

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "x-warp-ui-type": "WEB",
    "x-warp-ui-app": "BO",
    "x-warp-ui-ver": "1.0",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/backoffice/",
}

AUTH_QUERY = """
#prfrtkn:webbo:00feb1ab9d29a937aed5f99ad94a68e626b4f84e:a8b5cfc25504e5030a35c5e023c47e4700cc5c41
query authStrategyStart($type: AuthStrategyType!, $initialState: AuthStrategyInitialState!) {
  authStrategyStart(type: $type, initialState: $initialState) {
    strategy { strategyDescriptor stepDescriptor type }
    result {
      __typename
      ... on AuthStrategyResultSuccess {
        auth { loginUrl }
        step { __typename }
      }
      ... on AuthStrategyResultRetry {
        answer { errors { code message } }
      }
      ... on AuthStrategyResultError {
        answer { errors { code message } }
      }
    }
  }
}
"""

ME_QUERY = """
#prfrtkn:webbo:ae5bdf39b25057eaed276497911b303d04b365b8:c3cb6451d4eec098f4a78301fd32e5f9c52338f0
query getMe @domain(domains: [BO_PROFILE_HEADINFO]) {
  me { __typename }
}
"""

ORDERS_QUERY = """
#prfrtkn:webbo:edf00fb17884d25c7864bf0f695f5dd62bd3b6cf:33ac89346fb21e2a24afd7867841b9726d969e7a
query BoSearchBoardItems(
  $filter: BoSearchFrontFiltersInput!
  $useSavedFilter: Boolean
  $allVerticals: Boolean
  $pageSize: Int
  $sort: BoSearchSortEnum
) @domain(domains: [BO_BOARD, BO_BOARD_LIST]) {
  boSearchBoardItems(
    filter: $filter
    useSavedFilter: $useSavedFilter
    allVerticals: $allVerticals
    pageSize: $pageSize
    sort: $sort
  ) {
    items {
      id
      type
      ... on BoSearchSnippet {
        title
        description
        lastUpdateDate
        price { prefix value suffix }
      }
    }
  }
}
"""


class ProfiAuthenticationError(RuntimeError):
    pass


def _parse_published_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_price(price: Any) -> str:
    if not isinstance(price, dict):
        return "по договоренности"
    parts = [price.get("prefix"), price.get("value"), price.get("suffix")]
    formatted = " ".join(str(part).strip() for part in parts if part not in (None, ""))
    return formatted or "по договоренности"


def parse_projects(payload: dict[str, Any]) -> list[Project]:
    board = payload.get("data", {}).get("boSearchBoardItems", {})
    items = board.get("items", []) if isinstance(board, dict) else []
    projects: list[Project] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
            continue
        project_id = str(item["id"])
        projects.append(
            Project(
                source="Profi.ru",
                external_id=project_id,
                title=str(item["title"]),
                description=str(item.get("description") or ""),
                price=_format_price(item.get("price")),
                url=f"https://profi.ru/backoffice/n.php?o={project_id}",
                category=TARGET_CATEGORY_LABEL,
                published_at=_parse_published_at(item.get("lastUpdateDate")),
            )
        )
    return projects


class ProfiSource:
    def __init__(self, session: aiohttp.ClientSession, login: str, password: str) -> None:
        self._session = session
        self._login = login
        self._password = password
        self._authenticated = False
        self._disabled = False

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        async with self._session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers={
                **HEADERS,
                "X-Requested-With": "XMLHttpRequest",
                "x-wtf-id": str(uuid4()),
            },
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("errors"):
            messages = "; ".join(
                str(error.get("message", "GraphQL error")) for error in payload["errors"]
            )
            raise RuntimeError(f"Profi.ru API: {messages}")
        return payload

    async def _login_to_account(self) -> None:
        # Кабинет выставляет технические cookies до первого GraphQL-запроса.
        async with self._session.get(f"{BASE_URL}/backoffice/") as response:
            response.raise_for_status()
            await response.read()
        payload = await self._graphql(
            AUTH_QUERY,
            {
                "type": "password",
                "initialState": {
                    "implementer": {
                        "passwordCredentials": {
                            "login": self._login,
                            "password": self._password,
                        },
                        "siteId": "1815",
                    },
                    "currentHost": "profi.ru",
                },
            },
        )
        auth = payload.get("data", {}).get("authStrategyStart") or {}
        result = auth.get("result") or {}
        if result.get("__typename") != "AuthStrategyResultSuccess":
            errors = (result.get("answer") or {}).get("errors") or []
            message = "; ".join(str(error.get("message") or error.get("code")) for error in errors)
            raise ProfiAuthenticationError(message or "Profi.ru отклонил вход")
        if result.get("step") is not None:
            raise ProfiAuthenticationError(
                "Profi.ru запросил дополнительное подтверждение входа; "
                "нужна активная парольная авторизация"
            )
        login_url = (result.get("auth") or {}).get("loginUrl")
        if login_url:
            async with self._session.get(urljoin(BASE_URL, str(login_url))) as response:
                response.raise_for_status()
                await response.read()
        me = await self._graphql(ME_QUERY, {})
        if not (me.get("data", {}).get("me") or {}).get("__typename"):
            raise ProfiAuthenticationError("Не удалось подтвердить авторизацию Profi.ru")
        self._authenticated = True
        LOGGER.info("Profi.ru: авторизация выполнена")

    async def fetch(self) -> list[Project]:
        if self._disabled:
            return []
        if not self._authenticated:
            try:
                await self._login_to_account()
            except (aiohttp.ClientResponseError, ProfiAuthenticationError) as error:
                self._disabled = True
                LOGGER.error(
                    "Profi.ru отключён до перезапуска после ошибки авторизации: %s",
                    error,
                )
                return []
        try:
            payload = await self._graphql(
                ORDERS_QUERY,
                {
                    "filter": {},
                    "useSavedFilter": True,
                    "allVerticals": True,
                    "pageSize": 50,
                    "sort": "DEFAULT",
                },
            )
        except (aiohttp.ClientResponseError, RuntimeError):
            self._authenticated = False
            raise
        projects = parse_projects(payload)
        LOGGER.info("Profi.ru: найдено %d карточек по услугам аккаунта", len(projects))
        return projects
