import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from freelance_bot.sources.profi import (
    AUTH_QUERY,
    BASE_URL,
    ORDERS_QUERY,
    ProfiAuthenticationError,
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
    assert projects[0].published_at == datetime(2026, 7, 17, 15, 20, tzinfo=UTC)


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
    assert projects[0].published_at == datetime.fromtimestamp(1784301600, UTC)
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
    source._complete_login = AsyncMock()
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
    source._complete_login.assert_awaited_once_with("/backoffice/login/success")
    assert page.urls == []
    assert len(requests) == 1
    assert source._authenticated


@pytest.fixture
def browser_runtime(monkeypatch):
    """Fake only the browser transport; run the real source and auth lifecycle."""
    import playwright.async_api

    page = MagicMock()
    page.is_closed.return_value = False
    page.goto = AsyncMock()
    page.wait_for_function = AsyncMock()
    board = {
        "data": {
            "boSearchBoardItems": {
                "items": [{"id": "123", "title": "Создание сайта", "type": "SNIPPET"}]
            }
        }
    }
    auth = {
        "data": {
            "authStrategyStart": {
                "result": {
                    "__typename": "AuthStrategyResultSuccess",
                    "auth": {"loginUrl": "/backoffice/"},
                    "step": None,
                }
            }
        }
    }

    async def evaluate(_script, arg=None):
        if arg is None:
            return {"status": 200, "body": "{}"}
        if isinstance(arg, str):
            return {"status": 200, "ok": True}
        return {"status": 200, "body": json.dumps(auth if arg["query"] == AUTH_QUERY else board)}

    page.evaluate = AsyncMock(side_effect=evaluate)
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.storage_state = AsyncMock(
        return_value={
            "cookies": [
                {"name": "session", "value": "token", "domain": ".profi.ru", "path": "/"}
            ],
            "origins": [],
        }
    )
    context.close = AsyncMock()
    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    runtime = MagicMock()
    runtime.chromium.launch = AsyncMock(return_value=browser)
    runtime.stop = AsyncMock()
    manager = MagicMock()
    manager.start = AsyncMock(return_value=runtime)
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: manager)
    return runtime, browser, context, page


@pytest.mark.asyncio
async def test_manual_and_two_background_cycles_keep_live_browser(browser_runtime):
    runtime, browser, context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    try:
        assert len(await source.fetch()) == 1
        assert len(await source.fetch_for_manual_selection()) == 1
        assert len(await source.fetch()) == 1
        assert len(await source.fetch()) == 1
        runtime.chromium.launch.assert_awaited_once()
        browser.new_context.assert_awaited_once()
        browser.close.assert_not_awaited()
        auth_calls = [
            call
            for call in page.evaluate.call_args_list
            if len(call.args) > 1
            and isinstance(call.args[1], dict)
            and call.args[1]["query"] == AUTH_QUERY
        ]
        assert len(auth_calls) == 1
    finally:
        await source.close()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    runtime.stop.assert_awaited_once()
    await source.close()
    browser.close.assert_awaited_once()
    assert not source._authenticated


@pytest.mark.asyncio
async def test_browser_crash_starts_new_session_with_saved_auth(browser_runtime):
    runtime, browser, _context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    try:
        await source.fetch()
        browser.is_connected.return_value = False
        await source.fetch()
        assert runtime.chromium.launch.await_count == 2
        auth_calls = [
            call
            for call in page.evaluate.call_args_list
            if len(call.args) > 1
            and isinstance(call.args[1], dict)
            and call.args[1]["query"] == AUTH_QUERY
        ]
        assert len(auth_calls) == 1
        assert browser.new_context.await_args_list[1].kwargs["storage_state"]["cookies"]
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_browser_auth_state_survives_source_restart(browser_runtime, tmp_path):
    _runtime, browser, context, page = browser_runtime
    state_path = tmp_path / "profi-storage-state.json"
    first = ProfiSource(None, "login", "password", storage_state_path=state_path)
    await first.fetch()
    await first.close()

    second = ProfiSource(None, "login", "password", storage_state_path=state_path)
    try:
        await second.fetch()
        auth_calls = [
            call
            for call in page.evaluate.call_args_list
            if len(call.args) > 1
            and isinstance(call.args[1], dict)
            and call.args[1]["query"] == AUTH_QUERY
        ]
        assert len(auth_calls) == 1
        assert browser.new_context.await_args_list[1].kwargs["storage_state"]["cookies"]
        assert json.loads(state_path.read_text(encoding="utf-8"))["cookies"]
        assert context.storage_state.await_count >= 2
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_expired_auth_retries_once_in_same_browser(browser_runtime):
    runtime, _browser, _context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    try:
        await source.fetch()
        evaluate = page.evaluate.side_effect
        failed = False

        async def expire_once(script, arg=None):
            nonlocal failed
            if isinstance(arg, dict) and arg["query"] == ORDERS_QUERY and not failed:
                failed = True
                return {"status": 200, "body": '{"errors":[{"message":"Access denied"}]}'}
            return await evaluate(script, arg)

        page.evaluate.side_effect = expire_once
        assert len(await source.fetch_for_manual_selection()) == 1
        assert source._authenticated
        assert source._auth_failures == 0
        runtime.chromium.launch.assert_awaited_once()
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_repeated_denial_backs_off_even_when_login_succeeds(monkeypatch, browser_runtime):
    from freelance_bot.sources import profi

    clock = [100.0]
    monkeypatch.setattr(profi.time, "monotonic", lambda: clock[0])
    runtime, _browser, _context, page = browser_runtime
    evaluate = page.evaluate.side_effect

    async def deny_board(script, arg=None):
        if isinstance(arg, dict) and arg["query"] == ORDERS_QUERY:
            return {"status": 403, "body": "private response must not be logged"}
        return await evaluate(script, arg)

    page.evaluate.side_effect = deny_board
    source = ProfiSource(None, "login", "password")
    for failures in (1, 2):
        with pytest.raises(ProfiAuthenticationError, match="HTTP 403") as error:
            await source.fetch()
        assert "private response" not in str(error.value)
        assert not source._authenticated
        assert source._auth_failures == failures
        assert source._next_auth_attempt_at == clock[0] + 300 * 2 ** (failures - 1)
        attempts = runtime.chromium.launch.await_count
        with pytest.raises(ProfiAuthenticationError, match="повторный вход доступен"):
            await source.fetch()
        assert runtime.chromium.launch.await_count == attempts
        clock[0] = source._next_auth_attempt_at
    page.evaluate.side_effect = evaluate
    assert len(await source.fetch()) == 1
    assert source._auth_failures == 0
    await source.close()


@pytest.mark.asyncio
async def test_cancelled_fetch_closes_resources_and_allows_recovery(browser_runtime):
    runtime, _browser, _context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    await source.fetch()
    evaluate = page.evaluate.side_effect
    entered = asyncio.Event()

    async def hanging_request(*args):
        entered.set()
        await asyncio.Future()

    page.evaluate.side_effect = hanging_request
    task = asyncio.create_task(source.fetch())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source._browser_page is None
    runtime.stop.assert_awaited_once()
    page.evaluate.side_effect = evaluate
    assert len(await source.fetch()) == 1
    await source.close()


@pytest.mark.asyncio
async def test_login_exchange_calls_renew_then_touch(browser_runtime):
    _runtime, _browser, _context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    source._browser_page = page
    await source._complete_login("/auth/token/renew/?token=private")
    assert [call.args[1] for call in page.evaluate.call_args_list] == [
        f"{BASE_URL}/auth/token/renew/?token=private",
        f"{BASE_URL}/auth/token/touch",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_response",
    [
        {"status": 200, "ok": False},
        {"status": 401, "ok": False},
        {"status": 423, "ok": False},
    ],
)
async def test_login_exchange_failure_does_not_claim_authentication(
    browser_runtime, failed_response
):
    _runtime, _browser, _context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    source._browser_page = page
    page.evaluate.return_value = failed_response
    page.evaluate.side_effect = None
    with pytest.raises(ProfiAuthenticationError, match="не завершил вход") as error:
        await source._complete_login("/auth/token/renew/?token=private")
    assert "private" not in str(error.value)
    assert not source._authenticated
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_exchange_rejects_foreign_origin(browser_runtime):
    _runtime, _browser, _context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    source._browser_page = page
    with pytest.raises(ProfiAuthenticationError, match="неизвестный адрес"):
        await source._complete_login("https://example.org/auth/token/renew")
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_session_gets_full_bounded_login_budget(browser_runtime):
    _runtime, _browser, _context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    try:
        await source.fetch()
        evaluate = page.evaluate.side_effect
        denied = 0

        async def expire_session(script, arg=None):
            nonlocal denied
            if isinstance(arg, dict) and arg["query"] == ORDERS_QUERY and denied < 2:
                denied += 1
                return {"status": 401, "body": ""}
            return await evaluate(script, arg)

        page.evaluate.side_effect = expire_session
        assert len(await source.fetch()) == 1
        assert source._authenticated
        assert source._auth_failures == 0
        assert denied == 2
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_parallel_manual_and_background_requests_are_serialized(browser_runtime):
    _runtime, _browser, _context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    evaluate = page.evaluate.side_effect
    active = 0
    maximum = 0

    async def slow_request(script, arg=None):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        try:
            return await evaluate(script, arg)
        finally:
            active -= 1

    page.evaluate.side_effect = slow_request
    try:
        results = await asyncio.gather(source.fetch(), source.fetch_for_manual_selection())
        assert [len(result) for result in results] == [1, 1]
        assert maximum == 1
    finally:
        await source.close()


@pytest.mark.asyncio
async def test_partial_browser_start_failure_closes_resources(browser_runtime):
    runtime, browser, context, page = browser_runtime
    source = ProfiSource(None, "login", "password")
    page.goto.side_effect = RuntimeError("navigation failed")
    with pytest.raises(RuntimeError, match="navigation failed"):
        await source.fetch()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    runtime.stop.assert_awaited_once()
    assert source._browser_page is None
