import asyncio
from datetime import datetime, timedelta, timezone
import logging

import aiohttp

from freelance_bot.config import Settings
from freelance_bot.models import Project
from freelance_bot.sources.fl import FlSource
from freelance_bot.sources.kwork import KworkSource
from freelance_bot.sources.profi import ProfiSource
from freelance_bot.storage import ProjectStore
from freelance_bot.vk import (
    COMMAND_FL,
    COMMAND_KWORK,
    COMMAND_MENU,
    COMMAND_PROFI,
    COMMAND_RECENT,
    COMMAND_REJECTED,
    COMMAND_RESPONDED,
    COMMAND_RESPONSES,
    COMMAND_CLIENT_CHOSE_OTHER,
    COMMAND_CLIENT_REPLIED,
    COMMAND_CLEAR_STATISTICS,
    COMMAND_RESPONSE_PROJECT,
    COMMAND_SETTINGS,
    COMMAND_STATISTICS,
    COMMAND_TOGGLE_FL,
    COMMAND_TOGGLE_KWORK,
    COMMAND_TOGGLE_PROFI,
    BotCommand,
    VkBot,
    DISPLAY_TZ,
    format_message,
    keyboard_json,
    project_keyboard_json,
    recent_keyboard_json,
    response_detail_keyboard_json,
    responses_keyboard_json,
    settings_keyboard_json,
    statistics_keyboard_json,
)

LOGGER = logging.getLogger(__name__)
MAX_PROJECT_AGE = timedelta(hours=24)


def _is_fresh(project: Project, *, now: datetime | None = None) -> bool:
    if project.published_at is None:
        return False
    published = project.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - MAX_PROJECT_AGE <= published <= current + timedelta(hours=1)


async def _fetch_sources(
    fl_source: FlSource,
    kwork_source: KworkSource,
    profi_source: ProfiSource | None,
    fl_lock: asyncio.Lock,
    kwork_lock: asyncio.Lock,
    profi_lock: asyncio.Lock,
) -> dict[str, list[Project]]:
    async def locked_fetch(
        source: FlSource | KworkSource | ProfiSource, lock: asyncio.Lock
    ):
        async with lock:
            return await source.fetch()

    names = ["FL.ru", "Kwork"]
    fetches = [
        locked_fetch(fl_source, fl_lock),
        locked_fetch(kwork_source, kwork_lock),
    ]
    if profi_source is not None:
        names.append("Profi.ru")
        fetches.append(locked_fetch(profi_source, profi_lock))
    results = await asyncio.gather(*fetches, return_exceptions=True)
    projects: dict[str, list[Project]] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            LOGGER.error("Ошибка получения проектов %s: %s", name, result)
        else:
            projects[name] = result
    return projects


async def _monitor_projects(
    settings: Settings,
    store: ProjectStore,
    fl_source: FlSource,
    kwork_source: KworkSource,
    profi_source: ProfiSource | None,
    fl_lock: asyncio.Lock,
    kwork_lock: asyncio.Lock,
    profi_lock: asyncio.Lock,
    bot: VkBot,
) -> None:
    while True:
        projects_by_source = await _fetch_sources(
            fl_source,
            kwork_source,
            profi_source,
            fl_lock,
            kwork_lock,
            profi_lock,
        )
        for source, projects in projects_by_source.items():
            first_source_run = not store.is_source_initialized(source)
            for project in projects:
                if store.is_seen(project.key):
                    continue
                if first_source_run and not settings.send_existing_on_first_run:
                    store.mark_seen(
                        project.key, project.source, count_for_statistics=False
                    )
                    continue
                if not _is_fresh(project):
                    store.mark_seen(
                        project.key, project.source, count_for_statistics=False
                    )
                    LOGGER.info(
                        "Старый проект пропущен: %s — %s (%s)",
                        project.source,
                        project.title,
                        project.published_at,
                    )
                    continue
                if not store.notifications_enabled(source):
                    store.mark_seen(project.key, project.source)
                    LOGGER.info("Уведомления %s выключены; сохранено без отправки: %s", source, project.title)
                    continue
                try:
                    store.remember_project(project)
                    await bot.send(project)
                except Exception:
                    LOGGER.exception("Не удалось отправить %s", project.key)
                    continue
                store.mark_seen(project.key, project.source)
                LOGGER.info("Отправлено: %s — %s", project.source, project.title)
            if first_source_run:
                store.mark_source_initialized(source)
                LOGGER.info(
                    "%s: первичная выдача сохранена; дальше придут только новые проекты",
                    source,
                )
        await asyncio.sleep(settings.poll_interval_seconds)


def _latest_ten(projects: list[Project]) -> list[Project]:
    def sort_key(project: Project) -> tuple[float, int]:
        timestamp = project.published_at.timestamp() if project.published_at else 0.0
        numeric_id = int(project.external_id) if project.external_id.isdigit() else 0
        return timestamp, numeric_id

    return sorted(projects, key=sort_key, reverse=True)[:10]


def _format_statistics(store: ProjectStore) -> str:
    statistics = store.project_statistics()
    started_at = store.statistics_started_at().astimezone(DISPLAY_TZ)
    labels = (("day", "За 24 часа"), ("week", "За 7 дней"), ("month", "За 30 дней"))
    lines = [
        "📊 Статистика новых проектов",
        f"Отсчёт с {started_at:%d.%m.%Y %H:%M}",
    ]
    for period, label in labels:
        kwork = statistics["Kwork"][period]
        fl = statistics["FL.ru"][period]
        profi = statistics["Profi.ru"][period]
        lines.extend(
            (
                "",
                label,
                f"Kwork.ru — {kwork}",
                f"FL.ru — {fl}",
                f"Profi.ru — {profi}",
                f"Всего — {kwork + fl + profi}",
            )
        )
    return "\n".join(lines)


def _format_feedback_summary(store: ProjectStore) -> str:
    counts = store.feedback_counts()
    return "\n".join(
        (
            "📨 Отклики",
            "",
            f"Откликнулся — {counts['responded']}",
            f"Не подошло — {counts['rejected']}",
            f"Клиент написал — {counts['client_replied']}",
            f"Заказали у другого — {counts['client_chose_other']}",
        )
    )


async def _listen_for_commands(
    bot: VkBot,
    store: ProjectStore,
    fl_source: FlSource,
    kwork_source: KworkSource,
    profi_source: ProfiSource | None,
    fl_lock: asyncio.Lock,
    kwork_lock: asyncio.Lock,
    profi_lock: asyncio.Lock,
) -> None:
    command_lock = asyncio.Lock()

    async def edit_project_message(
        event: BotCommand, message: str, keyboard: str
    ) -> None:
        if event.event_id is not None and event.conversation_message_id is not None:
            try:
                await bot.edit_text(event.conversation_message_id, message, keyboard=keyboard)
            except Exception:
                LOGGER.warning(
                    "Не удалось обновить кнопки проекта VK", exc_info=True
                )

    async def show_menu() -> None:
        await bot.hide_ui()
        await bot.set_persistent_keyboard(keyboard_json())

    async def show_settings() -> None:
        await bot.hide_ui()
        await bot.set_persistent_keyboard(
            settings_keyboard_json(
                kwork_enabled=store.notifications_enabled("Kwork"),
                fl_enabled=store.notifications_enabled("FL.ru"),
                profi_enabled=store.notifications_enabled("Profi.ru"),
            )
        )

    async def show_responses() -> None:
        active = store.active_responses(limit=8)
        projects = [project for project, _ in active]
        if active:
            message = (
                f"{_format_feedback_summary(store)}\n\n"
                f"Активных откликов — {len(active)}.\nВыберите проект:"
            )
        else:
            message = (
                f"{_format_feedback_summary(store)}\n\n"
                "Активных откликов пока нет."
            )
        await bot.set_persistent_keyboard(responses_keyboard_json(projects))
        await bot.replace_ui(message)

    async def show_notice(message: str, keyboard: str) -> None:
        await bot.set_persistent_keyboard(keyboard)
        await bot.replace_ui(message)

    async def handle(event: BotCommand) -> None:
        command = event.name
        if command in {COMMAND_RESPONDED, COMMAND_REJECTED}:
            if event.project_key is None:
                return
            decision = "responded" if command == COMMAND_RESPONDED else "rejected"
            try:
                selected_decision = store.toggle_project_decision(
                    event.project_key, decision
                )
            except KeyError:
                LOGGER.warning("Проект не найден в локальной истории: %s", event.project_key)
                return
            project = store.get_project(event.project_key)
            if (
                project is not None
                and event.event_id is not None
                and event.conversation_message_id is not None
            ):
                await edit_project_message(
                    event,
                    format_message(project),
                    project_keyboard_json(project.key, selected_decision),
                )
            return
        if command in {COMMAND_CLIENT_REPLIED, COMMAND_CLIENT_CHOSE_OTHER}:
            if event.project_key is None:
                return
            outcome = (
                "client_replied"
                if command == COMMAND_CLIENT_REPLIED
                else "client_chose_other"
            )
            if not store.set_project_outcome(event.project_key, outcome):
                LOGGER.warning("Исход выбран без отклика: %s", event.project_key)
                return
            await show_responses()
            return
        if command == COMMAND_MENU:
            await show_menu()
            return
        if command == COMMAND_SETTINGS:
            await show_settings()
            return
        if command == COMMAND_STATISTICS:
            await show_notice(_format_statistics(store), statistics_keyboard_json())
            return
        if command == COMMAND_CLEAR_STATISTICS:
            store.reset_statistics()
            await show_notice(_format_statistics(store), statistics_keyboard_json())
            return
        if command == COMMAND_RESPONSES:
            await show_responses()
            return
        if command == COMMAND_RECENT:
            await bot.hide_ui()
            await bot.set_persistent_keyboard(recent_keyboard_json())
            return
        if command == COMMAND_RESPONSE_PROJECT:
            if event.project_key is None:
                await show_responses()
                return
            project = store.get_project(event.project_key)
            feedback = store.get_project_feedback(event.project_key)
            if project is None or feedback is None or feedback[0] != "responded":
                await show_responses()
                return
            status = (
                "💬 Клиент написал"
                if feedback[1] == "client_replied"
                else "⏳ Ждём ответа клиента"
            )
            await show_notice(
                f"{format_message(project)}\n\n{status}",
                response_detail_keyboard_json(project.key),
            )
            return
        if command in {COMMAND_TOGGLE_KWORK, COMMAND_TOGGLE_FL, COMMAND_TOGGLE_PROFI}:
            source = {
                COMMAND_TOGGLE_KWORK: "Kwork",
                COMMAND_TOGGLE_FL: "FL.ru",
                COMMAND_TOGGLE_PROFI: "Profi.ru",
            }[command]
            store.toggle_notifications(source)
            await show_settings()
            return
        if command not in {COMMAND_KWORK, COMMAND_FL, COMMAND_PROFI}:
            return
        if command_lock.locked():
            await show_notice("⏳ Предыдущий запрос ещё выполняется.", recent_keyboard_json())
            return
        async with command_lock:
            source_name = {
                COMMAND_KWORK: "Kwork",
                COMMAND_FL: "FL.ru",
                COMMAND_PROFI: "Profi.ru",
            }[command]
            if command == COMMAND_PROFI and profi_source is None:
                await show_notice(
                    "⚠️ Profi.ru ещё не настроен. Добавьте PROFI_LOGIN и "
                    "PROFI_PASSWORD в .env и перезапустите контейнер.",
                    recent_keyboard_json(),
                )
                return
            await show_notice(
                f"⏳ Загружаю последние проекты {source_name}…",
                recent_keyboard_json(),
            )
            try:
                if command == COMMAND_KWORK:
                    async with kwork_lock:
                        projects = await kwork_source.fetch()
                elif command == COMMAND_FL:
                    async with fl_lock:
                        projects = await fl_source.fetch()
                else:
                    assert profi_source is not None
                    async with profi_lock:
                        projects = await profi_source.fetch()
            except Exception:
                LOGGER.exception("Ошибка тестовой выдачи %s", source_name)
                await show_notice(
                    f"⚠️ Не удалось загрузить проекты {source_name}.",
                    recent_keyboard_json(),
                )
                return
            latest = _latest_ten(projects)
            if not latest:
                await show_notice(
                    f"В выбранных рубриках {source_name} проектов пока нет.",
                    recent_keyboard_json(),
                )
                return
            await bot.hide_ui()
            for project in reversed(latest):
                store.remember_project(project)
                feedback = store.get_project_feedback(project.key)
                await bot.send(
                    project,
                    test_view=True,
                    decision=feedback[0] if feedback is not None else None,
                )

    await bot.listen(handle)


async def run(settings: Settings) -> None:
    timeout = aiohttp.ClientTimeout(total=45)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FreelanceCategoryNotifier/1.0)"}
    store = ProjectStore(settings.database_path)
    kwork_source = KworkSource(settings.kwork_login, settings.kwork_password)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            fl_source = FlSource(session)
            profi_source = (
                ProfiSource(session, settings.profi_login, settings.profi_password)
                if settings.profi_login and settings.profi_password
                else None
            )
            if profi_source is None:
                LOGGER.warning(
                    "Profi.ru отключён: заполните PROFI_LOGIN и PROFI_PASSWORD в .env"
                )
            bot = VkBot(
                session,
                settings.vk_group_token,
                settings.vk_user_id,
                settings.vk_api_version,
            )
            fl_lock = asyncio.Lock()
            kwork_lock = asyncio.Lock()
            profi_lock = asyncio.Lock()
            if store.get_state("vk_keyboard_version") != "8":
                try:
                    await bot.clear_persistent_keyboard()
                except Exception:
                    LOGGER.warning("Не удалось скрыть старую клавиатуру VK", exc_info=True)
                try:
                    await bot.cleanup_old_navigation_messages()
                    await bot.set_persistent_keyboard(keyboard_json())
                    try:
                        await bot.refresh_recent_project_keyboards(
                            lambda project_key: (
                                feedback[0]
                                if (feedback := store.get_project_feedback(project_key))
                                is not None
                                else None
                            )
                        )
                    except Exception:
                        LOGGER.warning(
                            "Не удалось обновить кнопки старых проектов VK",
                            exc_info=True,
                        )
                    store.set_state("vk_keyboard_version", "8")
                except Exception:
                    LOGGER.warning(
                        "Не удалось установить постоянную клавиатуру VK",
                        exc_info=True,
                    )
            await asyncio.gather(
                _monitor_projects(
                    settings,
                    store,
                    fl_source,
                    kwork_source,
                    profi_source,
                    fl_lock,
                    kwork_lock,
                    profi_lock,
                    bot,
                ),
                _listen_for_commands(
                    bot,
                    store,
                    fl_source,
                    kwork_source,
                    profi_source,
                    fl_lock,
                    kwork_lock,
                    profi_lock,
                ),
            )
    finally:
        await kwork_source.close()
        store.close()


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
