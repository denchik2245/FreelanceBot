import asyncio
import logging
from datetime import UTC, datetime, timedelta

import aiohttp
from dotenv import load_dotenv

from freelance_bot.ai import ProjectAdvisor, create_advisor
from freelance_bot.config import Settings
from freelance_bot.config_texts import ConfigTextManager
from freelance_bot.fl_mail import GmailFlMailbox, format_fl_notification
from freelance_bot.keywords import matches_project_keywords
from freelance_bot.models import Project
from freelance_bot.project_filters import ProjectFilterManager
from freelance_bot.response_log import format_digest, format_panel
from freelance_bot.sources.fl import FlSource
from freelance_bot.sources.kwork import KworkSource
from freelance_bot.sources.profi import ProfiSource
from freelance_bot.storage import ProjectStore
from freelance_bot.tracker import DIGEST_STATE, digest_week
from freelance_bot.vk import (
    COMMAND_CHANGE_RESPONSE_COUNTER,
    COMMAND_CLEAR_CHAT,
    COMMAND_CLEAR_RESPONSE_STATISTICS,
    COMMAND_CLEAR_STATISTICS,
    COMMAND_CLIENT_CHOSE_OTHER,
    COMMAND_CLIENT_REPLIED,
    COMMAND_CONFIG_TEXTS,
    COMMAND_DELETE_RESPONSE,
    COMMAND_EDIT_FILTER,
    COMMAND_FILTER_SETTINGS,
    COMMAND_MENU,
    COMMAND_RESPONDED,
    COMMAND_RESPONSE_PROJECT,
    COMMAND_RESPONSE_SOURCE,
    COMMAND_RESPONSES,
    COMMAND_REWRITE_RESPONSE,
    COMMAND_SET_CONFIG_TEXT,
    COMMAND_SETTINGS,
    COMMAND_SOURCE_SETTINGS,
    COMMAND_STATISTICS,
    COMMAND_TOGGLE_FL,
    COMMAND_TOGGLE_KWORK,
    COMMAND_TOGGLE_PROFI,
    COMMAND_UPDATE_FILTER,
    COMMAND_VIEW_CONFIG_TEXT,
    COMMAND_WEEKLY_DIGEST,
    COMMAND_WRITE_RESPONSE,
    DISPLAY_TZ,
    BotCommand,
    VkBot,
    config_texts_keyboard_json,
    filter_settings_keyboard_json,
    format_message,
    keyboard_json,
    project_keyboard_json,
    responses_keyboard_json,
    settings_keyboard_json,
    source_settings_keyboard_json,
    statistics_keyboard_json,
)

LOGGER = logging.getLogger(__name__)
MAX_PROJECT_AGE = timedelta(hours=24)
FL_MAIL_UID_VALIDITY_STATE = "fl_mail:uid_validity"
FL_MAIL_LAST_UID_STATE = "fl_mail:last_uid"


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


def _assessment_input_changed(previous: Project | None, current: Project) -> bool:
    if previous is None:
        return True
    return (
        previous.title,
        previous.description,
        previous.category,
        previous.price,
    ) != (
        current.title,
        current.description,
        current.category,
        current.price,
    )


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
    advisor: ProjectAdvisor | None,
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
                previous_assessment = None
                if store.is_project_seen(project):
                    previous_assessment = store.get_ai_assessment(project.key)
                    if previous_assessment is None or previous_assessment.decision != "unclear":
                        continue
                if project.publication_id and store.is_seen(project.canonical_key):
                    LOGGER.info(
                        "Обнаружена повторная публикация: %s — %s (%s)",
                        project.source,
                        project.title,
                        project.published_at,
                    )
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
                assessment = None
                try:
                    previous_project = store.get_project(project.key)
                    store.remember_project(project)
                    if advisor is not None:
                        assessment = previous_assessment or store.get_ai_assessment(project.key)
                        if (
                            assessment is None
                            or not assessment.decision
                            or assessment.filter_revision != advisor.filter_revision
                            or (assessment.decision == "unclear" and
                                _assessment_input_changed(previous_project, project))
                        ):
                            assessment = await advisor.assess(project)
                            store.remember_ai_assessment(assessment)
                except Exception:
                    LOGGER.exception("Не удалось проанализировать %s", project.key)
                    # Never send an unchecked project. Leave it unseen so the next
                    # polling cycle can retry after a temporary GigaChat failure.
                    continue
                if assessment is not None and assessment.decision == "unclear":
                    LOGGER.info(
                        "AI оставил %s без решения; повторная оценка при изменении заказа "
                        "или правил: %s",
                        project.key,
                        assessment.reason,
                    )
                    continue
                if assessment is not None and assessment.decision != "accept":
                    store.mark_ai_rejected(project.key)
                    store.mark_seen(project.key, project.source)
                    LOGGER.info(
                        "AI отфильтровал %s: %s — %s",
                        project.key,
                        assessment.decision,
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


def _stored_int(store: ProjectStore, name: str) -> int | None:
    value = store.get_state(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        LOGGER.warning("Некорректное числовое состояние %s сброшено", name)
        return None


async def _monitor_fl_mail(
    settings: Settings,
    store: ProjectStore,
    mailbox: GmailFlMailbox,
    bot: VkBot,
) -> None:
    while True:
        try:
            batch = await mailbox.fetch(
                last_uid=_stored_int(store, FL_MAIL_LAST_UID_STATE),
                uid_validity=_stored_int(store, FL_MAIL_UID_VALIDITY_STATE),
            )
            delivered = 0
            for notification in batch.notifications:
                if store.is_mail_notification_seen(notification.key):
                    continue
                await bot.send_text(format_fl_notification(notification))
                store.mark_mail_notification_seen(notification.key)
                delivered += 1
            store.set_state(FL_MAIL_UID_VALIDITY_STATE, str(batch.uid_validity))
            store.set_state(FL_MAIL_LAST_UID_STATE, str(batch.last_uid))
            if batch.initialized:
                LOGGER.info(
                    "Gmail FL.ru подключён; текущие письма сохранены без уведомлений"
                )
            elif delivered:
                LOGGER.info("Отправлено уведомлений о сообщениях FL.ru: %d", delivered)
        except Exception:
            LOGGER.exception("Не удалось проверить уведомления FL.ru в Gmail")
        await asyncio.sleep(settings.fl_mail_poll_interval_seconds)


def _format_statistics(store: ProjectStore) -> str:
    statistics = store.project_statistics()
    ai_rejected = store.ai_rejected_statistics()
    started_at = store.statistics_started_at().astimezone(DISPLAY_TZ)
    labels = (("week", "За 7 дней"), ("month", "За 30 дней"))
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
    lines.extend(("", format_panel(store.responses)))
    return "\n".join(lines)


def _digest_due(store: ProjectStore, settings: Settings, now: datetime) -> bool:
    if settings.weekly_digest_hour < 0:
        return False
    local = now.astimezone(DISPLAY_TZ)
    if local.isoweekday() != 7 or local.hour < settings.weekly_digest_hour:
        return False
    return store.get_state(DIGEST_STATE) != digest_week(now)


async def _weekly_digest(settings: Settings, store: ProjectStore, bot: VkBot) -> None:
    while True:
        now = datetime.now(UTC)
        try:
            if _digest_due(store, settings, now):
                await bot.send_text(
                    format_digest(store.responses, now=now, display_tz=DISPLAY_TZ)
                )
                store.set_state(DIGEST_STATE, digest_week(now))
                LOGGER.info("Недельная сводка откликов отправлена")
        except Exception:
            LOGGER.exception("Не удалось отправить недельную сводку откликов")
        await asyncio.sleep(600)


async def _listen_for_commands(
    bot: VkBot,
    store: ProjectStore,
    fl_source: FlSource,
    kwork_source: KworkSource,
    profi_source: ProfiSource | None,
    fl_lock: asyncio.Lock,
    kwork_lock: asyncio.Lock,
    profi_lock: asyncio.Lock,
    advisor: ProjectAdvisor | None,
    config_texts: ConfigTextManager,
    project_filters: ProjectFilterManager,
) -> None:
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

    async def show_responses(note: str | None = None) -> None:
        message = format_panel(store.responses)
        if note:
            message += f"\n\n⚠️ {note}"
        await bot.set_persistent_keyboard(responses_keyboard_json())
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
        await show_notice(
            "🎯 Фильтры проектов\n\n"
            "AI отправляет только подтверждённые подходящие проекты.\n"
            f"Минимальный бюджет — {budget}\n\n"
            "Проекты без указанного бюджета не отбрасываются.",
            filter_settings_keyboard_json(
                min_score=current.min_ai_score,
                min_budget=current.min_budget,
            ),
        )

    async def show_filter_editor(name: str) -> None:
        current = project_filters.settings()
        instructions = {
            "score": (
                "Баллы больше не используются. AI отправляет только accept; "
                "критерии меняются в разделе AI-тексты → Промпт отбора."
            ),
            "budget": (
                f"Текущий минимальный бюджет: {current.min_budget} ₽\n\n"
                "Отправьте, например: /budget 10000\nДля отключения: /budget 0"
            ),
        }
        keyboard = filter_settings_keyboard_json(
            min_score=current.min_ai_score,
            min_budget=current.min_budget,
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
            f"Для длинного текста прикрепите UTF-8 .txt или .json к сообщению /set {key}.",
            config_texts_keyboard_json(),
        )

    async def handle(event: BotCommand) -> None:
        command = event.name
        if command == COMMAND_SOURCE_SETTINGS:
            await show_sources()
            return
        if command == COMMAND_FILTER_SETTINGS:
            await show_filter_settings()
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
                    await show_filter_editor("score")
                    return
                elif event.filter_name == "budget":
                    normalized = value.replace(" ", "")
                    project_filters.set_min_budget(0 if normalized == "off" else int(normalized))
                else:
                    raise ValueError("Неизвестная настройка фильтра")
            except (ValueError, TypeError) as error:
                await show_notice(f"⚠️ {error}", settings_keyboard_json())
                return
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
                    "⚠️ Укажите файл: /set filter.",
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
            # Old messages can still contain callbacks from the removed feature.
            await bot.send_text("Генерация откликов удалена из бота.", keyboard=False)
            return
        if command == COMMAND_RESPONDED:
            if event.project_key is None:
                return
            try:
                selected_decision = store.toggle_project_decision(event.project_key, "responded")
            except KeyError:
                LOGGER.warning("Проект не найден в локальной истории: %s", event.project_key)
                return
            except ValueError as error:
                await show_responses(str(error))
                return
            project = store.get_project(event.project_key)
            if project is not None:
                try:
                    if selected_decision == "responded":
                        store.responses.record(project, store.get_ai_assessment(project.key))
                    else:
                        store.responses.discard(project.key)
                except Exception:
                    LOGGER.exception("Не удалось записать отклик в трекер: %s", project.key)
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
        if command in {COMMAND_RESPONSE_SOURCE, COMMAND_CHANGE_RESPONSE_COUNTER}:
            # Old counter keyboards may still be open in the client after the upgrade.
            await show_responses()
            return
        if command == COMMAND_WEEKLY_DIGEST:
            await show_notice(
                format_digest(store.responses, display_tz=DISPLAY_TZ),
                responses_keyboard_json(),
            )
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
        if command == COMMAND_CLEAR_RESPONSE_STATISTICS:
            # The tracker keeps full history; the old reset button is gone.
            await show_responses()
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
        if command in {
            COMMAND_RESPONSE_PROJECT,
            COMMAND_DELETE_RESPONSE,
            COMMAND_CLIENT_REPLIED,
            COMMAND_CLIENT_CHOSE_OTHER,
        }:
            # Old keyboards may still exist in the chat after an upgrade.
            await show_responses()
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

    await bot.listen(handle)


async def run(settings: Settings) -> None:
    timeout = aiohttp.ClientTimeout(total=45)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; FreelanceCategoryNotifier/1.0)"}
    store = ProjectStore(settings.database_path)
    project_filters = ProjectFilterManager(store, default_ai_score=settings.ai_min_score)
    kwork_source = KworkSource(settings.kwork_login, settings.kwork_password)
    advisor: ProjectAdvisor | None = None
    config_texts = ConfigTextManager(
        filter_prompt_path=settings.ai_filter_prompt_path,
    )
    profi_source: ProfiSource | None = None
    try:
        if settings.ai_enabled:
            advisor = create_advisor(settings)
            await advisor.__aenter__()
            LOGGER.info(
                "AI включён: провайдер=%s, фильтр=%s, резерв=%s, только accept",
                settings.ai_provider,
                advisor.filter_model,
                advisor.filter_fallback_model,
            )
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            fl_source = FlSource(session)
            profi_source = (
                ProfiSource(
                    session,
                    settings.profi_login,
                    settings.profi_password,
                    storage_state_path=settings.database_path.parent
                    / "profi-storage-state.json",
                )
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
            fl_mailbox = (
                GmailFlMailbox(settings.fl_gmail_address, settings.fl_gmail_app_password)
                if settings.fl_gmail_address and settings.fl_gmail_app_password
                else None
            )
            if fl_mailbox is None:
                LOGGER.warning(
                    "Уведомления о сообщениях FL.ru отключены: заполните "
                    "FL_GMAIL_ADDRESS и FL_GMAIL_APP_PASSWORD в .env"
                )
            if store.get_state("vk_keyboard_version") != "13":
                try:
                    await bot.clear_persistent_keyboard()
                except Exception:
                    LOGGER.warning("Не удалось скрыть старую клавиатуру VK", exc_info=True)
                try:
                    await bot.cleanup_old_navigation_messages()
                    await bot.set_persistent_keyboard(keyboard_json())
                    try:
                        await bot.refresh_project_keyboards(
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
                    store.set_state("vk_keyboard_version", "13")
                except Exception:
                    LOGGER.warning(
                        "Не удалось установить постоянную клавиатуру VK",
                        exc_info=True,
                    )
            background_tasks = [
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
            ]
            background_tasks.append(_weekly_digest(settings, store, bot))
            if fl_mailbox is not None:
                background_tasks.append(
                    _monitor_fl_mail(settings, store, fl_mailbox, bot)
                )
            await asyncio.gather(*background_tasks)
    finally:
        if profi_source is not None:
            await profi_source.close()
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
