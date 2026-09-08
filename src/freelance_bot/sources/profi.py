import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import aiohttp

from freelance_bot.models import Project

LOGGER = logging.getLogger(__name__)
BASE_URL = "https://profi.ru"
GRAPHQL_URL = f"{BASE_URL}/graphql"
TARGET_CATEGORY_LABEL = "Услуги аккаунта Profi.ru"
PAGE_SIZE = 50
MAX_PAGES = 5
MANUAL_MAX_PAGES = 10
MAX_PROJECT_AGE = timedelta(hours=24)
AUTH_RETRY_BASE_SECONDS = 300
AUTH_RETRY_MAX_SECONDS = 3600
BROWSER_CHALLENGE_TIMEOUT_SECONDS = 15
BROWSER_REQUEST_TIMEOUT_SECONDS = 45

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "x-app-id": "BO",
    "x-new-auth-compatible": "1",
    "x-warp-consumer": "WEB",
    "x-warp-ui-type": "WEB",
    "x-warp-ui-app": "WEBBO",
    "x-warp-ui-ver": "1.0",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/backoffice/",
}

AUTH_QUERY = """#prfrtkn:webbo:d8306f0cb9b3586cde92113791c8f7dc637e221b:40e91e7164ba105f5941f4dfa18139771a439e06

      query authStrategyStart($type: AuthStrategyType!, $initialState: AuthStrategyInitialState!) {
  authStrategyStart(type: $type, initialState: $initialState) {
    ...AuthStrategyUseResultFragment
  }
}
      fragment AuthStrategyUseResultFragment on AuthStrategyUseResult {
  strategy {
    strategyDescriptor
    stepDescriptor
    name
    type
  }
  result {
    __typename
    ... on AuthStrategyResultRetry {
      answer {
        __typename
        errors {
          __typename
          code
          message
          param
        }
      }
    }
    ... on AuthStrategyResultError {
      answer {
        __typename
        errors {
          __typename
          code
          message
          param
        }
        askForSupport {
          title
          message
          chatMessage
          supportPayload {
            checkId
          }
        }
      }
    }
    ... on AuthStrategyResultSuccess {
      __typename
      answer {
        __typename
        events {
          __typename
          ... on AuthStrategyIAnalyticEvent {
            type
          }
        }
      }
      auth {
        loginUrl
        redirectUrl
      }
      step {
        ...AuthStrategyStepVariant
      }
    }
  }
}
fragment AuthStrategyStepVariant on IAuthStrategyStep {
  __typename
  stepId
  title
  ... on AuthStrategyStepFillPhone {
    phoneSuggestion
  }
  ... on AuthStrategyStepValidateMobileId {
    phoneNumber
    resendDelay
  }
  ... on AuthStrategyStepValidatePincode {
    phoneNumber
    resendDelay
  }
  ... on AuthStrategyStepValidatePinMobileId {
    phoneNumber
  }
  ... on AuthStrategyStepMobileIdNotification {
    timeout
  }
  ... on AuthStrategyStepFillUserInfo {
    requestedFields {
      __typename
      fieldId
      type
      required
      suggestedValue
    }
  }
  ... on AuthStrategyStepRequestSocNet {
    socNetId
    oAuthStateToken
    popupUrl
  }
  ... on AuthStrategyStepLoginMtsId {
    isSecondTry
    popupUrl
  }
  ... on AuthStrategyStepLogoutMtsId {
    socNetId
    oAuthStateToken
    popupUrl
  }
  ... on AuthStrategyStepRequestYandex {
    appId
    scopes
  }
}"""

ORDERS_QUERY = """#prfrtkn:webbo:edf00fb17884d25c7864bf0f695f5dd62bd3b6cf:33ac89346fb21e2a24afd7867841b9726d969e7a

      query BoSearchBoardItems($filter: BoSearchFrontFiltersInput!, $useSavedFilter: Boolean, $allVerticals: Boolean, $searchQuery: String, $searchEntities: [BoSearchEntityInput!], $searchId: ID, $nextCursor: String, $pageSize: Int, $boSortUp: Int, $minScore: Float, $coordinates: BoSearchAreaInput, $clusterId: ID, $sort: BoSearchSortEnum) @domain(domains: [BO_BOARD, BO_BOARD_LIST]) {
  boSearchBoardItems(
    filter: $filter
    useSavedFilter: $useSavedFilter
    allVerticals: $allVerticals
    searchQuery: $searchQuery
    searchEntities: $searchEntities
    searchId: $searchId
    nextCursor: $nextCursor
    pageSize: $pageSize
    boSortUp: $boSortUp
    minScore: $minScore
    coordinates: $coordinates
    clusterId: $clusterId
    sort: $sort
  ) {
    nextCursor
    serverTs
    totalCount
    analytics {
      boardSearchQuery
      boardSearchUsed
    }
    items {
      id
      type
      ... on BoSearchPremiumBlock {
        title
        description
        buttonLabel
      }
      ... on BoSearchPremiumRepeatBlock {
        title
      }
      ... on BoSearchSnippet {
        ...snippetFieldsCommon
        isFresh
        isStandard
        coordinates {
          lat
          lon
        }
        clientInfo {
          name
        }
        clientTags {
          value
        }
        badges {
          id
          imageKey
          label
        }
        status {
          text
          color
        }
        schedule
        images {
          host
          width
          height
          original
        }
      }
      ... on BoSearchEmptyState {
        view {
          title
          description
          imageKey
          button {
            label
            actionType
          }
        }
      }
      ... on BoSearchStories {
        id
        type
      }
      ... on BoSearchDivider {
        title
        button {
          label
          actionType
        }
      }
      ... on BoSearchCarousel {
        snippets {
          id
          isFresh
          ...snippetFieldsCommon
        }
      }
      ... on BoSearchSurvey {
        id
        type
        title
        surveyKey
        options {
          type
          title
          formId
        }
      }
      ... on BoSearchAdFoxBanner {
        adUnitId
      }
    }
  }
}
      fragment snippetFieldsCommon on BoSearchSnippet {
  score
  title
  description
  isReposted
  lastUpdateDate
  notice {
    label
    color
  }
  analyticsData {
    caseId
    score
  }
  geo {
    clientMayCome {
      address
      geoplaces {
        code
        color
        distance
        name
      }
      prefix
      suffix
    }
    orderLocation {
      address
      geoplaces {
        code
        color
        distance
        name
        prepDistance
      }
      prefix
      suffix
    }
    remote {
      address
      geoplaces {
        code
        color
        distance
        name
        prepDistance
      }
      prefix
      suffix
    }
  }
  price {
    prefix
    suffix
    value
  }
  secondPrice {
    prefix
    suffix
    value
  }
  headerIcon
  isViewed
  shouldRequestRefuseReasons
}"""


class ProfiAuthenticationError(RuntimeError):
    pass


def _parse_published_at(value: Any) -> datetime | None:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if value.replace(".", "", 1).isdigit():
            value = float(value)
        else:
            normalized = value.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, UTC)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _format_price(price: Any) -> str:
    if not isinstance(price, dict):
        return "по договоренности"
    parts = [price.get("prefix"), price.get("value"), price.get("suffix")]
    formatted = " ".join(str(part).strip() for part in parts if part not in (None, ""))
    return formatted or "по договоренности"


def _board(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    board = data.get("boSearchBoardItems")
    return board if isinstance(board, dict) else {}


def _snippet_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    snippets: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "CAROUSEL":
            nested = item.get("snippets")
            if isinstance(nested, list):
                snippets.extend(value for value in nested if isinstance(value, dict))
        elif item_type in (None, "SNIPPET"):
            snippets.append(item)
    return snippets


def parse_projects(payload: dict[str, Any]) -> list[Project]:
    unique: dict[str, Project] = {}
    for item in _snippet_items(_board(payload).get("items")):
        if not item.get("id") or not item.get("title"):
            continue
        project_id = str(item["id"])
        project = Project(
            source="Profi.ru",
            external_id=project_id,
            title=str(item["title"]),
            description=str(item.get("description") or ""),
            price=_format_price(item.get("price")),
            url=f"{BASE_URL}/backoffice/n.php?o={project_id}",
            category=TARGET_CATEGORY_LABEL,
            published_at=_parse_published_at(item.get("lastUpdateDate")),
        )
        unique.setdefault(project.key, project)
    return list(unique.values())


def _graphql_error_message(errors: Any) -> str:
    if not isinstance(errors, list):
        return "GraphQL error"
    messages = [
        str(error.get("message") or "GraphQL error") for error in errors if isinstance(error, dict)
    ]
    return "; ".join(messages) or "GraphQL error"


def _is_auth_graphql_error(errors: Any) -> bool:
    if not isinstance(errors, list):
        return False
    auth_codes = {"UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN", "AUTH_REQUIRED"}
    for error in errors:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        code = str(extensions.get("code", "")).upper() if isinstance(extensions, dict) else ""
        message = str(error.get("message", "")).lower()
        if code in auth_codes or any(
            marker in message
            for marker in (
                "unauthenticated",
                "unauthorized",
                "authentication required",
                "access denied",
            )
        ):
            return True
    return False


class ProfiSource:
    def __init__(self, session: aiohttp.ClientSession, login: str, password: str) -> None:
        self._session = session
        self._login = login
        self._password = password
        self._authenticated = False
        self._auth_flow_id = str(uuid4())
        self._auth_failures = 0
        self._next_auth_attempt_at = 0.0
        self._last_auth_error = ""
        self._browser_page: Any | None = None
        self._browser: Any | None = None
        self._browser_context: Any | None = None
        self._playwright: Any | None = None
        self._browser_lock = asyncio.Lock()

    @asynccontextmanager
    async def _browser_session(self):
        # Manual requests and background polling share the *live* page, including
        # its sessionStorage and token refresh scripts, not just a cookie snapshot.
        async with self._browser_lock:
            self._check_auth_backoff()
            try:
                await self._ensure_browser()
                yield
            except BaseException:
                await self._close_browser()
                raise

    async def close(self) -> None:
        async with self._browser_lock:
            await self._close_browser()

    async def _close_browser(self) -> None:
        context, browser, playwright = (self._browser_context, self._browser, self._playwright)
        self._browser_page = None
        self._browser_context = None
        self._browser = None
        self._playwright = None
        self._authenticated = False
        for resource, method in ((context, "close"), (browser, "close"), (playwright, "stop")):
            if resource is not None:
                try:
                    await getattr(resource, method)()
                except Exception:  # noqa: BLE001 - finish cleanup without logging session data
                    LOGGER.warning("Profi.ru: ошибка закрытия браузера")

    async def _ensure_browser(self) -> None:
        if (
            self._browser is not None
            and self._browser.is_connected()
            and self._browser_page is not None
            and not self._browser_page.is_closed()
        ):
            return
        await self._close_browser()
        try:
            from playwright.async_api import async_playwright
        except ImportError as error:
            raise RuntimeError(
                "Для Profi.ru не установлен Playwright; пересоберите Docker-образ"
            ) from error

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage"],
            )
            self._browser_context = await self._browser.new_context(
                locale="ru-RU",
            )
            page = await self._browser_context.new_page()
            await page.goto(
                f"{BASE_URL}/backoffice/",
                wait_until="domcontentloaded",
                timeout=BROWSER_CHALLENGE_TIMEOUT_SECONDS * 1000,
            )
            await page.wait_for_function(
                "document.cookie.includes('prfr_q_val=')",
                timeout=BROWSER_CHALLENGE_TIMEOUT_SECONDS * 1000,
            )
            self._browser_page = page
            LOGGER.info("Profi.ru: браузерная сессия подготовлена")
        except BaseException:
            await self._close_browser()
            raise

    async def _graphql(
        self,
        query: str,
        variables: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._browser_page is None:
            raise RuntimeError("GraphQL Profi.ru вызван без браузерной сессии")
        skipped_headers = {"accept-language", "origin", "referer", "user-agent"}
        request_headers = {
            key: value for key, value in HEADERS.items() if key.lower() not in skipped_headers
        }
        request_headers.update(headers or {})
        request_headers["x-wtf-id"] = str(uuid4())
        response = await asyncio.wait_for(
            self._browser_page.evaluate(
                """
            ({url, query, variables, headers}) => new Promise((resolve, reject) => {
              const xhr = new XMLHttpRequest();
              xhr.open('POST', url);
              xhr.withCredentials = true;
              xhr.timeout = 40000;
              for (const [key, value] of Object.entries(headers)) {
                xhr.setRequestHeader(key, value);
              }
              xhr.onload = () => resolve({status: xhr.status, body: xhr.responseText});
              xhr.onerror = () => reject(new Error('Profi.ru API network error'));
              xhr.ontimeout = () => reject(new Error('Profi.ru API timeout'));
              xhr.send(JSON.stringify({query, variables}));
            });
            """,
                {
                    "url": GRAPHQL_URL,
                    "query": query,
                    "variables": variables,
                    "headers": request_headers,
                },
            ),
            timeout=BROWSER_REQUEST_TIMEOUT_SECONDS,
        )
        status = int(response.get("status") or 0)
        body = str(response.get("body") or "")
        if status in {401, 403}:
            raise ProfiAuthenticationError(
                f"Profi.ru вернул HTTP {status}: требуется повторный вход"
            )
        if not 200 <= status < 300:
            raise RuntimeError(f"Profi.ru API вернул HTTP {status}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError("Profi.ru API вернул ответ не в формате JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeError("Profi.ru API вернул ответ неизвестного формата")  # noqa: TRY004
        errors = payload.get("errors")
        if errors:
            message = _graphql_error_message(errors)
            if _is_auth_graphql_error(errors):
                raise ProfiAuthenticationError(f"Profi.ru API: {message}")
            raise RuntimeError(f"Profi.ru API: {message}")
        return payload

    def _record_auth_failure(self, error: ProfiAuthenticationError) -> None:
        self._auth_failures += 1
        exponent = min(self._auth_failures - 1, 4)
        delay = min(
            AUTH_RETRY_BASE_SECONDS * 2**exponent,
            AUTH_RETRY_MAX_SECONDS,
        )
        self._next_auth_attempt_at = time.monotonic() + delay
        self._last_auth_error = str(error)
        LOGGER.error(
            "Profi.ru: вход не выполнен, следующая попытка через %d с: %s",
            delay,
            error,
        )

    async def _login_to_account(self) -> None:
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
                    "defaultOrderCityId": "prfr",
                    "currentHost": BASE_URL,
                },
            },
            headers={
                "X-Auth-Flow-Uuid": self._auth_flow_id,
                "x-warp-ui-app": "WEBBO",
                "x-warp-ui-ver": "100",
            },
        )
        auth = payload.get("data", {}).get("authStrategyStart") or {}
        result = auth.get("result") or {}
        if result.get("__typename") != "AuthStrategyResultSuccess":
            errors = (result.get("answer") or {}).get("errors") or []
            message = "; ".join(
                str(error.get("message") or error.get("code"))
                for error in errors
                if isinstance(error, dict)
            )
            raise ProfiAuthenticationError(message or "Profi.ru отклонил вход")
        if result.get("step") is not None:
            raise ProfiAuthenticationError(
                "Profi.ru запросил SMS, captcha или другое подтверждение входа"
            )
        login_url = (result.get("auth") or {}).get("loginUrl")
        if not login_url:
            raise ProfiAuthenticationError("Profi.ru не вернул адрес завершения входа")
        await self._complete_login(str(login_url))
        # Only a successful protected board request resets the failure backoff.
        self._authenticated = True
        LOGGER.info("Profi.ru: авторизация выполнена")

    async def _complete_login(self, login_url: str) -> None:
        url = urljoin(BASE_URL, login_url)
        if urlsplit(url).scheme != "https" or urlsplit(url).netloc != urlsplit(BASE_URL).netloc:
            raise ProfiAuthenticationError("Profi.ru вернул неизвестный адрес завершения входа")
        # The web client's LoginUrlCaller fetches loginUrl then /auth/token/touch.
        # Navigating to loginUrl omits the app headers and never completes login.
        await self._exchange_auth_tokens((url, f"{BASE_URL}/auth/token/touch"))

    async def _exchange_auth_tokens(self, endpoints: tuple[str, ...]) -> None:
        assert self._browser_page is not None
        # Use the same XHR transport as the site's LoginUrlFetcher so the page's
        # normal response handlers can update the browser session.
        for endpoint in endpoints:
            response = await asyncio.wait_for(
                self._browser_page.evaluate(
                    """
                    async url => {
                      for (let attempt = 0; attempt < 3; attempt++) {
                        const response = await new Promise((resolve, reject) => {
                          const xhr = new XMLHttpRequest();
                          xhr.open('GET', url);
                          xhr.withCredentials = true;
                          xhr.timeout = 10000;
                          const headers = {
                            'x-new-auth-compatible': '1',
                            'x-app-id': 'BO',
                            'x-warp-consumer': 'WEB',
                            'x-warp-ui-type': 'WEB',
                            'x-warp-ui-app': 'WEBBO',
                            'x-warp-ui-ver': '1.0',
                          };
                          for (const [key, value] of Object.entries(headers)) {
                            xhr.setRequestHeader(key, value);
                          }
                          xhr.onload = () => resolve({status: xhr.status, body: xhr.responseText});
                          xhr.onerror = () => reject(new Error('Profi.ru login network error'));
                          xhr.ontimeout = () => reject(new Error('Profi.ru login timeout'));
                          xhr.send();
                        });
                        let ok = false;
                        try { ok = JSON.parse(response.body).result === 'ok'; } catch {}
                        if (response.status !== 423 || attempt === 2) {
                          return {status: response.status, ok};
                        }
                        await new Promise(resolve => setTimeout(resolve, 1000));
                      }
                    }
                    """,
                    endpoint,
                ),
                timeout=BROWSER_REQUEST_TIMEOUT_SECONDS,
            )
            if response.get("status") != 200 or response.get("ok") is not True:
                raise ProfiAuthenticationError(
                    f"Profi.ru не завершил вход ({urlsplit(endpoint).path}, "
                    f"HTTP {response.get('status')})"
                )

    def _check_auth_backoff(self) -> None:
        retry_after = self._next_auth_attempt_at - time.monotonic()
        if retry_after > 0:
            raise ProfiAuthenticationError(
                f"повторный вход доступен через {retry_after:.0f} с: {self._last_auth_error}"
            )

    async def _ensure_authenticated(self) -> None:
        self._check_auth_backoff()
        if self._authenticated:
            return
        self._auth_flow_id = str(uuid4())
        try:
            await self._exchange_auth_tokens((f"{BASE_URL}/auth/token/init",))
            await self._login_to_account()
        except ProfiAuthenticationError as error:
            self._record_auth_failure(error)
            raise

    async def _authorized_graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self._check_auth_backoff()
        login_attempts = 0
        while True:
            if not self._authenticated:
                await self._ensure_authenticated()
                login_attempts += 1
            try:
                payload = await self._graphql(query, variables)
                break
            except ProfiAuthenticationError as error:
                self._authenticated = False
                # Apply the same bounded login budget to a fresh session and to
                # an expired one. An optimistic cached flag isn't a login attempt.
                if login_attempts >= 2:
                    self._record_auth_failure(error)
                    raise
        # A login response alone doesn't prove access to the protected board.
        self._auth_failures = 0
        self._next_auth_attempt_at = 0.0
        self._last_auth_error = ""
        return payload

    async def _fetch_projects(
        self,
        *,
        max_pages: int,
        cutoff: datetime | None,
        feed_label: str,
    ) -> list[Project]:
        unique: dict[str, Project] = {}
        cursor: str | None = None
        used_cursors: set[str] = set()
        for page in range(1, max_pages + 1):
            payload = await self._authorized_graphql(
                ORDERS_QUERY,
                {
                    "filter": {},
                    "useSavedFilter": True,
                    "allVerticals": False,
                    "nextCursor": cursor,
                    "pageSize": PAGE_SIZE,
                    "sort": "DEFAULT",
                },
            )
            if not isinstance(payload.get("data"), dict) or not isinstance(
                payload["data"].get("boSearchBoardItems"), dict
            ):
                raise RuntimeError("Profi.ru API не вернул доску заказов")  # noqa: TRY004
            page_projects = parse_projects(payload)
            for project in page_projects:
                unique.setdefault(project.key, project)
            LOGGER.info(
                "Profi.ru: %s, страница %d — найдено %d карточек",
                feed_label,
                page,
                len(page_projects),
            )
            dates = [project.published_at for project in page_projects if project.published_at]
            if cutoff is not None and dates and max(dates) < cutoff:
                break
            next_cursor = _board(payload).get("nextCursor")
            if not next_cursor:
                break
            cursor = str(next_cursor)
            if cursor in used_cursors:
                LOGGER.warning("Profi.ru: API повторил cursor, пагинация остановлена")
                break
            used_cursors.add(cursor)
        LOGGER.info("Profi.ru: %s — найдено %d уникальных карточек", feed_label, len(unique))
        return list(unique.values())

    async def fetch(self) -> list[Project]:
        async with self._browser_session():
            return await self._fetch_projects(
                max_pages=MAX_PAGES,
                cutoff=datetime.now(UTC) - MAX_PROJECT_AGE,
                feed_label="сохранённый фильтр",
            )

    async def fetch_for_manual_selection(self) -> list[Project]:
        async with self._browser_session():
            return await self._fetch_projects(
                max_pages=MANUAL_MAX_PAGES,
                cutoff=None,
                feed_label="ручная AI-выдача",
            )
