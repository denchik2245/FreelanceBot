import asyncio
import logging
from datetime import UTC, datetime, timedelta

import aiohttp
from dotenv import load_dotenv

from freelance_bot.ai import GigaChatProjectAdvisor
from freelance_bot.config import Settings
from freelance_bot.config_texts import ConfigTextManager
from freelance_bot.keywords import matches_project_keywords
from freelance_bot.models import AiAssessment, Project
from freelance_bot.project_filters import ProjectFilterManager
from freelance_bot.sources.fl import FlSource
from freelance_bot.sources.kwork import KworkSource
from freelance_bot.sources.profi import ProfiSource
from freelance_bot.storage import ProjectStore
from freelance_bot.vk import (
    COMMAND_CLEAR_CHAT,
    COMMAND_CLEAR_STATISTICS,
    COMMAND_CLIENT_CHOSE_OTHER,
    COMMAND_CLIENT_REPLIED,
    COMMAND_CONFIG_TEXTS,
    COMMAND_EDIT_FILTER,
    COMMAND_FL,
    COMMAND_FILTER_SETTINGS,
    COMMAND_KWORK,
    COMMAND_MENU,
    COMMAND_PROFI,
    COMMAND_QUIET_SETTINGS,
    COMMAND_RECENT,
    COMMAND_REJECTED,
    COMMAND_REWRITE_RESPONSE,
    COMMAND_RESPONDED,
    COMMAND_RESPONSE_PROJECT,
    COMMAND_RESPONSES,
    COMMAND_SETTINGS,
    COMMAND_SOURCE_SETTINGS,
    COMMAND_STATISTICS,
    COMMAND_TOGGLE_FL,
    COMMAND_TOGGLE_KWORK,
    COMMAND_TOGGLE_PROFI,
    COMMAND_SET_CONFIG_TEXT,
    COMMAND_VIEW_CONFIG_TEXT,
    COMMAND_WRITE_RESPONSE,
    COMMAND_UPDATE_FILTER,
    DISPLAY_TZ,
    BotCommand,
    VkBot,
    config_texts_keyboard_json,
    format_message,
    filter_settings_keyboard_json,
    keyboard_json,
    project_keyboard_json,
    recent_keyboard_json,
    response_detail_keyboard_json,
    response_variants_keyboard_json,
    responses_keyboard_json,
    settings_keyboard_json,
    quiet_settings_keyboard_json,
    source_settings_keyboard_json,
    statistics_keyboard_json,
)

LOGGER = logging.getLogger(__name__)
MAX_PROJECT_AGE = timedelta(hours=24)


def _keyword_candidates(source: str, projects: list[Project]) -> list[Project]:
    if source not in {"Kwork", "FL.ru"}:
        return projects
    candidates = [project for project in projects if matches_project_keywords(project)]
    LOGGER.info(
        "%s: ключевой фильтр пропустил %d из %d проектов",
        source,
        len(candidates),
        len(projects),
    )
    return candidates


def _is_fresh(project: Project, *, now: datetime | None = None) -> bool:
    if project.published_at is None:
        return False
    published = project.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return current - MAX_PROJECT_AGE <= published <= current + timedelta(hours=1)


async def _fetch_sources(
    fl_source: FlSource,
    kwork_source: KworkSource,
    profi_source: ProfiSource | None,
    fl_lock: asyncio.Lock,
    kwork_lock: asyncio.Lock,
    profi_lock: asyncio.Lock,
) -> dict[str, list[Project]]:
    async def locked_fetch(source: FlSource | KworkSource | ProfiSource, lock: asyncio.Lock):
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
            projects[name] = _keyword_candidates(name, result)
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
    advisor: GigaChatProjectAdvisor | None,
    project_filters: ProjectFilterManager,
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
                    store.mark_seen(project.key, project.source, count_for_statistics=False)
                    continue
                if not _is_fresh(project):
                    store.mark_seen(project.key, project.source, count_for_statistics=False)
                    LOGGER.info(
                        "Старый проект пропущен: %s — %s (%s)",
                        project.source,
                        project.title,
                        project.published_at,
                    )
                    continue
                if not store.notifications_enabled(source):
                    store.mark_seen(project.key, project.source)
                    LOGGER.info(
                        "Уведомления %s выключены; сохранено без отправки: %s",
                        source,
                        project.title,
                    )
                    continue
                rejection_reason = project_filters.rejection_reason(project)
                if rejection_reason is not None:
                    store.mark_seen(project.key, project.source, count_for_statistics=False)
                    LOGGER.info(
                        "Пользовательский фильтр пропустил %s: %s",
                        project.key,
                        rejection_reason,
                    )
                    continue
                if project_filters.is_quiet_now(datetime.now(DISPLAY_TZ)):
                    LOGGER.debug("Тихие часы: %s оставлен в очереди", project.key)
                    continue
                assessment = None
                try:
                    store.remember_project(project)
                    if advisor is not None:
                        assessment = store.get_ai_assessment(project.key)
                        if assessment is None or not assessment.summary:
                            assessment = await advisor.assess(project)
                            store.remember_ai_assessment(assessment)
                except Exception:
                    LOGGER.exception("Не удалось проанализировать %s", project.key)
                    # Never send an unchecked project. Leave it unseen so the next
                    # polling cycle can retry after a temporary GigaChat failure.
                    continue
                if (
                    assessment is not None
                    and assessment.score < project_filters.settings().min_ai_score
                ):
                    store.mark_ai_rejected(project.key)
                    store.mark_seen(project.key, project.source)
                    LOGGER.info(
                        "AI отфильтровал %s: %d/100 — %s",
                        project.key,
                        assessment.score,
                        assessment.reason,
                    )
                    continue
                try:
                    await bot.send(project, assessment=assessment)
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


def _latest_five(projects: list[Project]) -> list[Project]:
    def sort_key(project: Project) -> tuple[float, int]:
        timestamp = project.published_at.timestamp() if project.published_at else 0.0
        numeric_id = int(project.external_id) if project.external_id.isdigit() else 0
        return timestamp, numeric_id

    return sorted(projects, key=sort_key, reverse=True)[:5]


async def _latest_suitable_projects(
    projects: list[Project],
    store: ProjectStore,
    advisor: GigaChatProjectAdvisor,
    *,
    limit: int = 5,
    project_filters: ProjectFilterManager | None = None,
) -> list[tuple[Project, AiAssessment]]:
    """Walk newest-first until enough successfully assessed suitable projects are found."""
    selected: list[tuple[Project, AiAssessment]] = []
    candidates = sorted(
        projects,
        key=lambda item: (
            item.published_at.timestamp() if item.published_at else 0.0,
            int(item.external_id) if item.external_id.isdigit() else 0,
        ),
        reverse=True,
    )
    for project in candidates:
        if project_filters is not None and project_filters.rejection_reason(project) is not None:
            continue
        store.remember_project(project)
        assessment = store.get_ai_assessment(project.key)
        if assessment is None or not assessment.summary:
            try:
                assessment = await advisor.assess(project)
                store.remember_ai_assessment(assessment)
            except Exception:
                LOGGER.exception("Не удалось оценить проект ручной выдачи %s", project.key)
                continue
        suitable = (
            assessment.score >= project_filters.settings().min_ai_score
            if project_filters is not None
            else assessment.suitable
        )
        if not suitable:
            store.mark_ai_rejected(project.key)
            LOGGER.info(
                "AI отфильтровал проект ручной выдачи %s: %d/100 — %s",
                project.key,
                assessment.score,
                assessment.reason,
            )
            continue
        selected.append((project, assessment))
        if len(selected) == limit:
            break
    return selected


def _format_statistics(store: ProjectStore) -> str:
    statistics = store.project_statistics()
    ai_rejected = store.ai_rejected_statistics()
    daily_statistics = store.daily_project_statistics(display_timezone=DISPLAY_TZ)
    started_at = store.statistics_started_at().astimezone(DISPLAY_TZ)
    labels = (("day", "За 24 часа"), ("week", "За 7 дней"), ("month", "За 30 дней"))
    lines = [
        "📊 Статистика подходящих проектов",
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
                f"Всего подходящих — {kwork + fl + profi}",
                f"Отклонено AI — {ai_rejected[period]}",
            )
        )
    lines.extend(("", "По дням с начала месяца"))
    weekend_labels = {5: "суббота", 6: "воскресенье"}
    for index, (day, counts) in enumerate(reversed(daily_statistics.items())):
        if index:
            lines.append("")
        weekend = weekend_labels.get(day.weekday())
        date_label = f"{day:%d.%m.%Y}" + (f" ({weekend})" if weekend else "")
        total = counts["Kwork"] + counts["FL.ru"] + counts["Profi.ru"]
        lines.append(f"{date_label} — подходящих {total}")
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
    advisor: GigaChatProjectAdvisor | None,
    config_texts: ConfigTextManager,
    project_filters: ProjectFilterManager,
) -> None:
    command_lock = asyncio.Lock()
    response_lock = asyncio.Lock()

    async def edit_project_message(event: BotCommand, message: str, keyboard: str) -> None:
        if event.event_id is not None and event.conversation_message_id is not None:
            try:
                await bot.edit_text(event.conversation_message_id, message, keyboard=keyboard)
            except Exception:
                LOGGER.warning("Не удалось обновить кнопки проекта VK", exc_info=True)

    async def show_menu() -> None:
        await bot.hide_ui()
        await bot.set_persistent_keyboard(keyboard_json())

    async def show_settings() -> None:
        await bot.hide_ui()
        await bot.set_persistent_keyboard(settings_keyboard_json())

    async def show_sources() -> None:
        await bot.hide_ui()
        await bot.set_persistent_keyboard(
            source_settings_keyboard_json(
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
            message = f"{_format_feedback_summary(store)}\n\nАктивных откликов пока нет."
        await bot.set_persistent_keyboard(responses_keyboard_json(projects))
        await bot.replace_ui(message)

    async def show_notice(message: str, keyboard: str) -> None:
        await bot.set_persistent_keyboard(keyboard)
        await bot.replace_ui(message)

    async def show_filter_settings() -> None:
        current = project_filters.settings()
        budget = (
            f"{current.min_budget:,} ₽".replace(",", " ")
            if current.min_budget
            else "выключен"
        )
        include = ", ".join(current.include_keywords) or "не заданы"
        exclude = ", ".join(current.exclude_keywords) or "не заданы"
        await show_notice(
            "🎯 Фильтры проектов\n\n"
            f"Минимальный AI-балл — {current.min_ai_score}\n"
            f"Минимальный бюджет — {budget}\n"
            f"Желательные слова — {include}\n"
            f"Исключающие слова — {exclude}\n\n"
            "Проекты без указанного бюджета не отбрасываются.",
            filter_settings_keyboard_json(
                min_score=current.min_ai_score,
                min_budget=current.min_budget,
            ),
        )

    async def show_quiet_settings() -> None:
        current = project_filters.settings()
        enabled = current.quiet_start is not None and current.quiet_end is not None
        schedule = (
            f"с {current.quiet_start:02d}:00 до {current.quiet_end:02d}:00 (МСК+2)"
            if enabled
            else "выключены"
        )
        await show_notice(
            "🌙 Тихие часы\n\n"
            f"Сейчас: {schedule}.\n\n"
            "Во время тихих часов новые проекты остаются в очереди и отправляются позже, "
            "если им ещё нет 24 часов.",
            quiet_settings_keyboard_json(enabled=enabled),
        )

    async def show_filter_editor(name: str) -> None:
        current = project_filters.settings()
        instructions = {
            "score": (
                f"Текущий минимальный AI-балл: {current.min_ai_score}\n\n"
                "Отправьте, например: /score 75"
            ),
            "budget": (
                f"Текущий минимальный бюджет: {current.min_budget} ₽\n\n"
                "Отправьте, например: /budget 10000\nДля отключения: /budget 0"
            ),
            "include": (
                "Текущие желательные слова: "
                f"{', '.join(current.include_keywords) or 'не заданы'}\n\n"
                "Отправьте: /include figma, tilda, интернет-магазин\n"
                "Для очистки: /include off"
            ),
            "exclude": (
                "Текущие исключающие слова: "
                f"{', '.join(current.exclude_keywords) or 'не заданы'}\n\n"
                "Отправьте: /exclude wordpress, seo, парсинг\n"
                "Для очистки: /exclude off"
            ),
            "quiet": (
                "Отправьте часы начала и конца по МСК+2, например: /quiet 23 8\n"
                "Для отключения: /quiet off"
            ),
        }
        keyboard = (
            quiet_settings_keyboard_json(enabled=current.quiet_start is not None)
            if name == "quiet"
            else filter_settings_keyboard_json(
                min_score=current.min_ai_score,
                min_budget=current.min_budget,
            )
        )
        await show_notice(f"✏️ Изменение настройки\n\n{instructions[name]}", keyboard)

    async def show_config_texts() -> None:
        await show_notice(
            "📝 AI-тексты\n\nВыберите файл для просмотра и инструкции по замене.",
            config_texts_keyboard_json(),
        )

    async def show_config_text(key: str) -> None:
        item = config_texts.get(key)
        current = config_texts.read(key).strip()
        preview_limit = 2_700
        preview = current[:preview_limit]
        if len(current) > preview_limit:
            preview += "\n… (показано не полностью)"
        await show_notice(
            f"📝 {item.label}\n"
            f"Файл: {item.path.name} · {len(current)} символов\n\n"
            f"{preview}\n\n"
            f"Чтобы заменить, отправьте одним сообщением:\n"
            f"/set {key}\nНОВЫЙ ТЕКСТ\n\n"
            f"Для длинного текста прикрепите UTF-8 .txt к сообщению /set {key}.",
            config_texts_keyboard_json(),
        )

    async def refresh_project_description(project: Project) -> Project:
        if project.description:
            return project
        try:
            if project.source == "Kwork":
                async with kwork_lock:
                    projects = await kwork_source.fetch()
            elif project.source == "FL.ru":
                async with fl_lock:
                    projects = await fl_source.fetch()
            elif project.source == "Profi.ru" and profi_source is not None:
                async with profi_lock:
                    projects = await profi_source.fetch()
            else:
                return project
        except Exception:
            LOGGER.warning(
                "Не удалось обновить описание проекта %s перед AI-откликом",
                project.key,
                exc_info=True,
            )
            return project
        refreshed = next((item for item in projects if item.key == project.key), None)
        if refreshed is not None:
            store.remember_project(refreshed)
            return refreshed
        return project

    async def handle(event: BotCommand) -> None:
        command = event.name
        if command == COMMAND_SOURCE_SETTINGS:
            await show_sources()
            return
        if command == COMMAND_FILTER_SETTINGS:
            await show_filter_settings()
            return
        if command == COMMAND_QUIET_SETTINGS:
            await show_quiet_settings()
            return
        if command == COMMAND_EDIT_FILTER:
            if event.filter_name is None:
                await show_filter_settings()
            else:
                await show_filter_editor(event.filter_name)
            return
        if command == COMMAND_UPDATE_FILTER:
            try:
                value = (event.content or "").strip()
                if event.filter_name == "score":
                    project_filters.set_min_ai_score(int(value))
                    if advisor is not None:
                        advisor.set_min_score(int(value))
                elif event.filter_name == "budget":
                    normalized = value.replace(" ", "")
                    project_filters.set_min_budget(0 if normalized == "off" else int(normalized))
                elif event.filter_name in {"include", "exclude"}:
                    project_filters.set_keywords(
                        event.filter_name,
                        "" if value.casefold() in {"off", "нет", "-"} else value,
                    )
                elif event.filter_name == "quiet":
                    if value.casefold() in {"off", "нет", "-"}:
                        project_filters.set_quiet_hours(None, None)
                    else:
                        hours = [int(part) for part in value.replace(":", " ").split()]
                        if len(hours) != 2:
                            raise ValueError("Используйте формат /quiet 23 8")
                        project_filters.set_quiet_hours(hours[0], hours[1])
                else:
                    raise ValueError("Неизвестная настройка фильтра")
            except (ValueError, TypeError) as error:
                await show_notice(f"⚠️ {error}", settings_keyboard_json())
                return
            if event.filter_name == "quiet":
                await show_quiet_settings()
            else:
                await show_filter_settings()
            return
        if command == COMMAND_CONFIG_TEXTS:
            await show_config_texts()
            return
        if command == COMMAND_VIEW_CONFIG_TEXT:
            if event.config_key is None:
                await show_config_texts()
                return
            try:
                await show_config_text(event.config_key)
            except Exception:
                LOGGER.exception("Не удалось показать AI-текст %s", event.config_key)
                await show_notice(
                    "⚠️ Не удалось прочитать файл.", config_texts_keyboard_json()
                )
            return
        if command == COMMAND_SET_CONFIG_TEXT:
            if event.config_key is None:
                await show_notice(
                    "⚠️ Укажите файл: /set profile, /set filter или /set response.",
                    config_texts_keyboard_json(),
                )
                return
            try:
                content = event.content
                if event.document_url is not None:
                    content = await bot.download_text_document(event.document_url)
                if content is None:
                    raise ValueError("Добавьте новый текст после команды или прикрепите .txt")
                applied = config_texts.replace(event.config_key, content)
                if advisor is not None:
                    advisor.update_config_text(event.config_key, applied)
                item = config_texts.get(event.config_key)
            except (ValueError, RuntimeError, OSError, aiohttp.ClientError) as error:
                LOGGER.warning("Не удалось заменить AI-текст: %s", error)
                await show_notice(f"⚠️ {error}", config_texts_keyboard_json())
                return
            await show_notice(
                f"✅ {item.label} обновлён ({len(applied)} символов).\n"
                "Изменение уже применяется к новым запросам AI. "
                "Предыдущая версия сохранена рядом в .bak.",
                config_texts_keyboard_json(),
            )
            return
        if command in {COMMAND_WRITE_RESPONSE, COMMAND_REWRITE_RESPONSE}:
            if event.project_key is None:
                return
            if response_lock.locked():
                await bot.send_text(
                    "⏳ Предыдущий вариант отклика ещё создаётся.", keyboard=False
                )
                return
            project = store.get_project(event.project_key)
            if project is None:
                LOGGER.warning("Проект для AI-отклика не найден: %s", event.project_key)
                await bot.send_text("⚠️ Проект не найден в локальной истории.", keyboard=False)
                return
            assessment = store.get_ai_assessment(project.key)
            if advisor is None:
                await bot.send_text(
                    "⚠️ GigaChat отключён. Проверьте AI_ENABLED и ключ API.",
                    keyboard=False,
                )
                return
            project = await refresh_project_description(project)
            previous_response = store.get_ai_response(project.key)
            if command == COMMAND_REWRITE_RESPONSE and (
                not previous_response or event.response_action is None
            ):
                await bot.send_text(
                    "⚠️ Исходный отклик не найден. Создайте его заново из карточки проекта.",
                    keyboard=False,
                )
                return
            async with response_lock:
                try:
                    if command == COMMAND_WRITE_RESPONSE and assessment is None:
                        try:
                            assessment = await advisor.assess(project)
                            store.remember_ai_assessment(assessment)
                        except Exception:
                            LOGGER.warning(
                                "AI-оценка %s недоступна; пишу отклик без неё",
                                project.key,
                                exc_info=True,
                            )
                    response_text = await advisor.generate_response(
                        project,
                        previous_response=previous_response,
                        variation=(
                            event.response_action
                            if command == COMMAND_REWRITE_RESPONSE
                            else None
                        ),
                    )
                    store.remember_ai_response(
                        project.key,
                        response_text,
                        advisor.response_model,
                    )
                except Exception:
                    LOGGER.exception("Не удалось создать AI-отклик для %s", project.key)
                    await bot.send_text(
                        "⚠️ Не удалось написать отклик. Попробуйте нажать кнопку ещё раз.",
                        keyboard=False,
                    )
                    return
            variants_keyboard = response_variants_keyboard_json(project.key)
            if (
                command == COMMAND_REWRITE_RESPONSE
                and event.event_id is not None
                and event.conversation_message_id is not None
            ):
                try:
                    await bot.edit_text(
                        event.conversation_message_id,
                        response_text,
                        keyboard=variants_keyboard,
                    )
                    return
                except Exception:
                    LOGGER.warning(
                        "Не удалось заменить сообщение с AI-откликом", exc_info=True
                    )
            await bot.send_text(response_text, keyboard=variants_keyboard)
            return
        if command in {COMMAND_RESPONDED, COMMAND_REJECTED}:
            if event.project_key is None:
                return
            decision = "responded" if command == COMMAND_RESPONDED else "rejected"
            try:
                selected_decision = store.toggle_project_decision(event.project_key, decision)
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
                    format_message(
                        project,
                        assessment=store.get_ai_assessment(project.key),
                    ),
                    project_keyboard_json(project.key, selected_decision),
                )
            return
        if command in {COMMAND_CLIENT_REPLIED, COMMAND_CLIENT_CHOSE_OTHER}:
            if event.project_key is None:
                return
            outcome = (
                "client_replied" if command == COMMAND_CLIENT_REPLIED else "client_chose_other"
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
        if command == COMMAND_CLEAR_CHAT:
            try:
                deleted, failed = await bot.clear_outgoing_messages()
            except Exception:
                LOGGER.exception("Не удалось очистить сообщения бота в VK")
                await bot.send_text(
                    "⚠️ Не удалось очистить чат. Попробуйте ещё раз.",
                    keyboard=settings_keyboard_json(),
                )
                return
            result = f"✅ Удалено сообщений бота: {deleted}."
            if failed:
                result += f" Не удалось удалить: {failed} — VK ограничил их удаление."
            await bot.send_text(result, keyboard=keyboard_json())
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
                "💬 Клиент написал" if feedback[1] == "client_replied" else "⏳ Ждём ответа клиента"
            )
            await show_notice(
                f"{format_message(project, assessment=store.get_ai_assessment(project.key))}"
                f"\n\n{status}",
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
            await show_sources()
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
                        projects = await kwork_source.fetch_for_manual_selection()
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
            candidates = _keyword_candidates(source_name, projects)
            if not candidates:
                await show_notice(
                    f"В общей ленте {source_name} совпадений по ключевым словам пока нет.",
                    recent_keyboard_json(),
                )
                return
            if advisor is None:
                await show_notice(
                    "⚠️ GigaChat отключён, поэтому выбрать подходящие проекты нельзя.",
                    recent_keyboard_json(),
                )
                return
            await bot.hide_ui()
            selected = await _latest_suitable_projects(
                candidates,
                store,
                advisor,
                project_filters=project_filters,
            )
            if not selected:
                await show_notice(
                    f"Среди последних проектов {source_name} AI не нашёл подходящих.",
                    recent_keyboard_json(),
                )
                return
            for project, assessment in reversed(selected):
                feedback = store.get_project_feedback(project.key)
                await bot.send(
                    project,
                    test_view=True,
                    decision=feedback[0] if feedback is not None else None,
                    assessment=assessment,
                )

    await bot.listen(handle)


async def run(settings: Settings) -> None:
    timeout = aiohttp.ClientTimeout(total=45)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FreelanceCategoryNotifier/1.0)"}
    store = ProjectStore(settings.database_path)
    project_filters = ProjectFilterManager(store, default_ai_score=settings.ai_min_score)
    kwork_source = KworkSource(settings.kwork_login, settings.kwork_password)
    advisor: GigaChatProjectAdvisor | None = None
    config_texts = ConfigTextManager(
        profile_path=settings.ai_profile_path,
        filter_prompt_path=settings.ai_filter_prompt_path,
        response_prompt_path=settings.ai_response_prompt_path,
    )
    try:
        if settings.ai_enabled:
            advisor = GigaChatProjectAdvisor.from_paths(
                profile_path=settings.ai_profile_path,
                filter_prompt_path=settings.ai_filter_prompt_path,
                response_prompt_path=settings.ai_response_prompt_path,
                credentials=settings.gigachat_credentials,
                scope=settings.gigachat_scope,
                base_url=settings.gigachat_base_url,
                ca_bundle_file=settings.gigachat_ca_bundle_file,
                filter_model=settings.gigachat_filter_model,
                response_model=settings.gigachat_response_model,
                min_score=settings.ai_min_score,
            )
            await advisor.__aenter__()
            advisor.set_min_score(project_filters.settings().min_ai_score)
            LOGGER.info(
                "AI включён: фильтр=%s, отклики=%s, порог=%d",
                settings.gigachat_filter_model,
                settings.gigachat_response_model,
                project_filters.settings().min_ai_score,
            )
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            fl_source = FlSource(session)
            profi_source = (
                ProfiSource(session, settings.profi_login, settings.profi_password)
                if settings.profi_login and settings.profi_password
                else None
            )
            if profi_source is None:
                LOGGER.warning("Profi.ru отключён: заполните PROFI_LOGIN и PROFI_PASSWORD в .env")
            bot = VkBot(
                session,
                settings.vk_group_token,
                settings.vk_user_id,
                settings.vk_api_version,
            )
            fl_lock = asyncio.Lock()
            kwork_lock = asyncio.Lock()
            profi_lock = asyncio.Lock()
            if store.get_state("vk_keyboard_version") != "10":
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
                                if (feedback := store.get_project_feedback(project_key)) is not None
                                else None
                            )
                        )
                    except Exception:
                        LOGGER.warning(
                            "Не удалось обновить кнопки старых проектов VK",
                            exc_info=True,
                        )
                    store.set_state("vk_keyboard_version", "10")
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
                    advisor,
                    project_filters,
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
                    advisor,
                    config_texts,
                    project_filters,
                ),
            )
    finally:
        if advisor is not None:
            await advisor.__aexit__(None, None, None)
        await kwork_source.close()
        store.close()


def main() -> None:
    load_dotenv()
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
