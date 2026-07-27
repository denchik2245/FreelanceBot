import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any

import aiohttp

from freelance_bot.models import AiAssessment, Project

LOGGER = logging.getLogger(__name__)
COMMAND_KWORK = "last_kwork"
COMMAND_FL = "last_fl"
COMMAND_PROFI = "last_profi"
COMMAND_MENU = "menu"
COMMAND_SETTINGS = "settings"
COMMAND_STATISTICS = "statistics"
COMMAND_CLEAR_STATISTICS = "clear_statistics"
COMMAND_CLEAR_CHAT = "clear_chat"
COMMAND_RESPONSES = "responses"
COMMAND_RECENT = "recent"
COMMAND_TOGGLE_KWORK = "toggle_kwork"
COMMAND_TOGGLE_FL = "toggle_fl"
COMMAND_TOGGLE_PROFI = "toggle_profi"
COMMAND_RESPONDED = "responded"
COMMAND_REJECTED = "rejected"
COMMAND_WRITE_RESPONSE = "write_response"
COMMAND_CLIENT_REPLIED = "client_replied"
COMMAND_CLIENT_CHOSE_OTHER = "client_chose_other"
COMMAND_RESPONSE_PROJECT = "response_project"
DISPLAY_TZ = timezone(timedelta(hours=5))
ALL_COMMANDS = {
    COMMAND_KWORK,
    COMMAND_FL,
    COMMAND_PROFI,
    COMMAND_MENU,
    COMMAND_SETTINGS,
    COMMAND_STATISTICS,
    COMMAND_CLEAR_STATISTICS,
    COMMAND_CLEAR_CHAT,
    COMMAND_RESPONSES,
    COMMAND_RECENT,
    COMMAND_TOGGLE_KWORK,
    COMMAND_TOGGLE_FL,
    COMMAND_TOGGLE_PROFI,
    COMMAND_RESPONDED,
    COMMAND_REJECTED,
    COMMAND_WRITE_RESPONSE,
    COMMAND_CLIENT_REPLIED,
    COMMAND_CLIENT_CHOSE_OTHER,
    COMMAND_RESPONSE_PROJECT,
}


@dataclass(frozen=True, slots=True)
class BotCommand:
    name: str
    project_key: str | None = None
    peer_id: int | None = None
    conversation_message_id: int | None = None
    event_id: str | None = None


class VkApiError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"VK API {code}: {message}")


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def format_message(
    project: Project,
    *,
    test_view: bool = False,
    assessment: AiAssessment | None = None,
) -> str:
    del test_view  # Тестовая и автоматическая выдача намеренно выглядят одинаково.
    source = "Kwork.ru" if project.source == "Kwork" else project.source
    lines = [
        source,
        "",
        _clip(project.title, 180),
        f"💰 {project.price or 'по договоренности'}",
    ]
    if project.published_at is not None:
        published = project.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        lines.append(f"🕒 {published.astimezone(DISPLAY_TZ):%d.%m.%Y %H:%M}")
    lines.extend(("", project.url))
    if assessment is not None:
        lines.extend(
            (
                "",
                f"🤖 Оценка: {assessment.score}/100",
                f"Почему: {_clip(assessment.reason, 500)}",
            )
        )
    return "\n".join(lines)


def keyboard_json() -> str:
    def button(label: str, command: str) -> dict[str, Any]:
        return {
            "action": {
                "type": "callback",
                "label": label,
                "payload": json.dumps({"command": command}, ensure_ascii=False),
            },
            "color": "secondary",
        }

    keyboard = {
        "one_time": False,
        "inline": False,
        "buttons": [
            [button("🕘 Последние проекты", COMMAND_RECENT)],
            [
                button("⚙ Настройки", COMMAND_SETTINGS),
                button("📊 Статистика", COMMAND_STATISTICS),
            ],
            [button("📨 Отклики", COMMAND_RESPONSES)],
        ],
    }
    return json.dumps(keyboard, ensure_ascii=False, separators=(",", ":"))


def recent_keyboard_json() -> str:
    def button(label: str, command: str) -> dict[str, Any]:
        return {
            "action": {
                "type": "callback",
                "label": label,
                "payload": json.dumps({"command": command}, ensure_ascii=False),
            },
            "color": "secondary",
        }

    return json.dumps(
        {
            "one_time": False,
            "inline": False,
            "buttons": [
                [
                    button("Последние 5 Kwork", COMMAND_KWORK),
                    button("Последние 5 FL.ru", COMMAND_FL),
                ],
                [button("Последние 5 Profi.ru", COMMAND_PROFI)],
                [button("← Главное меню", COMMAND_MENU)],
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def project_keyboard_json(project_key: str, decision: str | None = None) -> str:
    def button(label: str, command: str, color: str) -> dict[str, Any]:
        return {
            "action": {
                "type": "callback",
                "label": label,
                "payload": json.dumps(
                    {"command": command, "project_key": project_key},
                    ensure_ascii=False,
                ),
            },
            "color": color,
        }

    return json.dumps(
        {
            "one_time": False,
            "inline": True,
            "buttons": [
                [
                    button(
                        "✅ Откликнулся" if decision == "responded" else "Откликнулся",
                        COMMAND_RESPONDED,
                        "positive" if decision == "responded" else "secondary",
                    ),
                    button(
                        "❌ Не подошло" if decision == "rejected" else "Не подошло",
                        COMMAND_REJECTED,
                        "negative" if decision == "rejected" else "secondary",
                    ),
                ],
                [button("✍ Написать отклик", COMMAND_WRITE_RESPONSE, "primary")],
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def response_keyboard_json(project_key: str) -> str:
    def button(label: str, command: str, color: str) -> dict[str, Any]:
        return {
            "action": {
                "type": "callback",
                "label": label,
                "payload": json.dumps(
                    {"command": command, "project_key": project_key},
                    ensure_ascii=False,
                ),
            },
            "color": color,
        }

    return json.dumps(
        {
            "one_time": False,
            "inline": True,
            "buttons": [
                [button("💬 Клиент написал", COMMAND_CLIENT_REPLIED, "positive")],
                [button("Заказали у другого", COMMAND_CLIENT_CHOSE_OTHER, "negative")],
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def response_detail_keyboard_json(project_key: str) -> str:
    keyboard = json.loads(response_keyboard_json(project_key))
    keyboard["inline"] = False
    keyboard["buttons"].append(
        [
            {
                "action": {
                    "type": "callback",
                    "label": "← К откликам",
                    "payload": json.dumps({"command": COMMAND_RESPONSES}, ensure_ascii=False),
                },
                "color": "secondary",
            }
        ]
    )
    return json.dumps(keyboard, ensure_ascii=False, separators=(",", ":"))


def settings_keyboard_json(*, kwork_enabled: bool, fl_enabled: bool, profi_enabled: bool) -> str:
    def toggle(label: str, command: str, enabled: bool) -> dict[str, Any]:
        status = "✅ вкл" if enabled else "⛔ выкл"
        return {
            "action": {
                "type": "callback",
                "label": f"{label}: {status}",
                "payload": json.dumps({"command": command}, ensure_ascii=False),
            },
            "color": "positive" if enabled else "negative",
        }

    back = {
        "action": {
            "type": "callback",
            "label": "← Главное меню",
            "payload": json.dumps({"command": COMMAND_MENU}, ensure_ascii=False),
        },
        "color": "secondary",
    }
    clear_chat = {
        "action": {
            "type": "callback",
            "label": "🗑 Очистить чат",
            "payload": json.dumps({"command": COMMAND_CLEAR_CHAT}, ensure_ascii=False),
        },
        "color": "negative",
    }
    keyboard = {
        "one_time": False,
        "inline": False,
        "buttons": [
            [toggle("Kwork", COMMAND_TOGGLE_KWORK, kwork_enabled)],
            [toggle("FL.ru", COMMAND_TOGGLE_FL, fl_enabled)],
            [toggle("Profi.ru", COMMAND_TOGGLE_PROFI, profi_enabled)],
            [clear_chat],
            [back],
        ],
    }
    return json.dumps(keyboard, ensure_ascii=False, separators=(",", ":"))


def back_keyboard_json() -> str:
    return json.dumps(
        {
            "one_time": False,
            "inline": False,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "callback",
                            "label": "← Главное меню",
                            "payload": json.dumps({"command": COMMAND_MENU}, ensure_ascii=False),
                        },
                        "color": "secondary",
                    }
                ]
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def statistics_keyboard_json() -> str:
    def button(label: str, command: str, color: str) -> dict[str, Any]:
        return {
            "action": {
                "type": "callback",
                "label": label,
                "payload": json.dumps({"command": command}, ensure_ascii=False),
            },
            "color": color,
        }

    return json.dumps(
        {
            "one_time": False,
            "inline": False,
            "buttons": [
                [button("🗑 Очистить статистику", COMMAND_CLEAR_STATISTICS, "negative")],
                [button("← Главное меню", COMMAND_MENU, "secondary")],
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def responses_keyboard_json(projects: list[Project]) -> str:
    buttons: list[list[dict[str, Any]]] = []
    for index, project in enumerate(projects[:8], start=1):
        buttons.append(
            [
                {
                    "action": {
                        "type": "callback",
                        "label": f"{index}. {_clip(project.title, 32)}",
                        "payload": json.dumps(
                            {
                                "command": COMMAND_RESPONSE_PROJECT,
                                "project_key": project.key,
                            },
                            ensure_ascii=False,
                        ),
                    },
                    "color": "secondary",
                }
            ]
        )
    buttons.append(json.loads(back_keyboard_json())["buttons"][0])
    return json.dumps(
        {"one_time": False, "inline": False, "buttons": buttons},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_command(message: dict[str, Any]) -> BotCommand | None:
    payload = message.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None
    if isinstance(payload, dict):
        command = payload.get("command")
        if command in ALL_COMMANDS:
            project_key = payload.get("project_key")
            return BotCommand(
                str(command),
                str(project_key) if isinstance(project_key, str) and project_key else None,
                int(message["peer_id"]) if message.get("peer_id") is not None else None,
                int(message["conversation_message_id"])
                if message.get("conversation_message_id") is not None
                else None,
                str(message["event_id"]) if message.get("event_id") else None,
            )

    text = str(message.get("text", "")).strip().casefold()
    text_commands = {
        "последние 5 kwork": COMMAND_KWORK,
        "последние 5 fl.ru": COMMAND_FL,
        "последние 5 profi.ru": COMMAND_PROFI,
        "последние 10 kwork": COMMAND_KWORK,
        "последние 10 fl.ru": COMMAND_FL,
        "последние 10 profi.ru": COMMAND_PROFI,
        "/menu": COMMAND_MENU,
        "меню": COMMAND_MENU,
        "настройки": COMMAND_SETTINGS,
        "статистика": COMMAND_STATISTICS,
        "отклики": COMMAND_RESPONSES,
        "последние проекты": COMMAND_RECENT,
    }
    command = text_commands.get(text)
    return (
        BotCommand(
            command,
            peer_id=int(message["peer_id"]) if message.get("peer_id") is not None else None,
            conversation_message_id=int(message["conversation_message_id"])
            if message.get("conversation_message_id") is not None
            else None,
        )
        if command
        else None
    )


class VkBot:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        user_id: int,
        api_version: str,
    ) -> None:
        self._session = session
        self._token = token
        self._user_id = user_id
        self._api_version = api_version
        self._group_id: int | None = None
        self._ui_message_id: int | None = None

    async def _api(self, method: str, **params: Any) -> Any:
        payload = {
            "access_token": self._token,
            "v": self._api_version,
            **params,
        }
        async with self._session.post(
            f"https://api.vk.com/method/{method}", data=payload
        ) as response:
            response.raise_for_status()
            data = await response.json()
        if "error" in data:
            error = data["error"]
            raise VkApiError(
                int(error.get("error_code", 0)),
                str(error.get("error_msg", "Unknown error")),
            )
        return data.get("response")

    async def send_text(self, message: str, *, keyboard: bool | str = True) -> bool:
        """Send a message and return whether the requested keyboard was attached."""
        params: dict[str, Any] = {
            "user_id": self._user_id,
            "random_id": random.SystemRandom().randint(1, 2_147_483_647),
            "message": message,
        }
        if keyboard:
            params["keyboard"] = keyboard_json() if keyboard is True else keyboard
        try:
            await self._api("messages.send", **params)
        except VkApiError as error:
            if not keyboard or error.code != 912:
                raise
            LOGGER.warning(
                "VK отклонил клавиатуру: функция чат-бота выключена; "
                "сообщение будет отправлено без кнопок"
            )
            params.pop("keyboard", None)
            params["random_id"] = random.SystemRandom().randint(1, 2_147_483_647)
            await self._api("messages.send", **params)
            return False
        return True

    async def edit_text(self, conversation_message_id: int, message: str, *, keyboard: str) -> None:
        await self._api(
            "messages.edit",
            peer_id=self._user_id,
            conversation_message_id=conversation_message_id,
            message=message,
            keyboard=keyboard,
        )

    async def delete_incoming(self, conversation_message_id: int) -> None:
        try:
            await self._api(
                "messages.delete",
                peer_id=self._user_id,
                conversation_message_ids=conversation_message_id,
                delete_for_all=1,
            )
        except VkApiError as error:
            LOGGER.debug("VK не разрешил удалить сообщение-команду: %s", error)

    async def answer_event(self, event: BotCommand) -> None:
        if event.event_id is None or event.peer_id is None:
            return
        await self._api(
            "messages.sendMessageEventAnswer",
            event_id=event.event_id,
            user_id=self._user_id,
            peer_id=event.peer_id,
        )

    async def clear_persistent_keyboard(self) -> None:
        """Remove the old reply keyboard without leaving a service message behind."""
        message_id = await self._api(
            "messages.send",
            user_id=self._user_id,
            random_id=random.SystemRandom().randint(1, 2_147_483_647),
            message="Обновляю меню…",
            keyboard=json.dumps(
                {"one_time": True, "inline": False, "buttons": []},
                separators=(",", ":"),
            ),
        )
        if isinstance(message_id, int):
            try:
                await self._api("messages.delete", message_ids=message_id, delete_for_all=1)
            except VkApiError:
                LOGGER.debug("Не удалось удалить служебное сообщение", exc_info=True)

    async def set_persistent_keyboard(self, keyboard: str) -> None:
        """Change the lower keyboard through a message that is deleted immediately."""
        message_id = await self._api(
            "messages.send",
            user_id=self._user_id,
            random_id=random.SystemRandom().randint(1, 2_147_483_647),
            message="\u2063",
            keyboard=keyboard,
        )
        if isinstance(message_id, int):
            await self._api("messages.delete", message_ids=message_id, delete_for_all=1)

    async def hide_ui(self) -> None:
        if self._ui_message_id is None:
            return
        message_id = self._ui_message_id
        self._ui_message_id = None
        try:
            await self._api("messages.delete", message_ids=message_id, delete_for_all=1)
        except VkApiError:
            LOGGER.debug("Не удалось удалить предыдущую панель", exc_info=True)

    async def replace_ui(self, message: str) -> None:
        """Keep at most one visible informational panel in the dialog."""
        await self.hide_ui()
        message_id = await self._api(
            "messages.send",
            user_id=self._user_id,
            random_id=random.SystemRandom().randint(1, 2_147_483_647),
            message=message,
        )
        self._ui_message_id = message_id if isinstance(message_id, int) else None

    async def cleanup_old_navigation_messages(self, *, limit: int = 100) -> None:
        response = await self._api("messages.getHistory", user_id=self._user_id, count=limit)
        items = response.get("items", []) if isinstance(response, dict) else []
        exact = {
            "Выберите действие:",
            "Панель управления",
            "🕘 Последние проекты",
        }
        prefixes = (
            "⚙ Настройки уведомлений",
            "Уведомления ",
            "Показал последние ",
            "⏳ Загружаю последние проекты ",
            "📨 Отклики\n",
            "📊 Статистика новых проектов",
        )
        message_ids = [
            int(item["id"])
            for item in items
            if isinstance(item, dict)
            and item.get("out")
            and item.get("id") is not None
            and (
                str(item.get("text", "")) in exact or str(item.get("text", "")).startswith(prefixes)
            )
        ]
        if message_ids:
            await self._api(
                "messages.delete",
                message_ids=",".join(map(str, message_ids)),
                delete_for_all=1,
            )

    async def clear_outgoing_messages(
        self, *, page_size: int = 200, batch_size: int = 100
    ) -> tuple[int, int]:
        """Delete this bot's visible messages from the private conversation."""
        message_ids: list[int] = []
        offset = 0
        while True:
            response = await self._api(
                "messages.getHistory",
                user_id=self._user_id,
                count=page_size,
                offset=offset,
            )
            items = response.get("items", []) if isinstance(response, dict) else []
            if not isinstance(items, list) or not items:
                break
            message_ids.extend(
                int(item["id"])
                for item in items
                if isinstance(item, dict) and item.get("out") and item.get("id") is not None
            )
            offset += len(items)
            total = int(response.get("count", offset)) if isinstance(response, dict) else offset
            if offset >= total:
                break
            await asyncio.sleep(0.34)

        deleted = 0
        failed = 0
        for start in range(0, len(message_ids), batch_size):
            batch = message_ids[start : start + batch_size]
            try:
                result = await self._api(
                    "messages.delete",
                    message_ids=",".join(map(str, batch)),
                    delete_for_all=1,
                )
            except VkApiError:
                LOGGER.warning("VK не разрешил удалить часть старых сообщений", exc_info=True)
                failed += len(batch)
                continue
            if isinstance(result, dict):
                batch_deleted = sum(
                    bool(result.get(str(message_id), result.get(message_id)))
                    for message_id in batch
                )
                deleted += batch_deleted
                failed += len(batch) - batch_deleted
            else:
                deleted += len(batch)
            if start + batch_size < len(message_ids):
                await asyncio.sleep(0.34)
        self._ui_message_id = None
        return deleted, failed

    async def refresh_recent_project_keyboards(
        self, decision_for: Callable[[str], str | None], *, limit: int = 100
    ) -> None:
        """Migrate recently sent project cards to the current button states."""
        response = await self._api("messages.getHistory", user_id=self._user_id, count=limit)
        items = response.get("items", []) if isinstance(response, dict) else []
        for item in items:
            if not isinstance(item, dict) or not item.get("out"):
                continue
            keyboard = item.get("keyboard")
            if not isinstance(keyboard, dict):
                continue
            project_key: str | None = None
            for row in keyboard.get("buttons", []):
                for button in row if isinstance(row, list) else []:
                    action = button.get("action", {}) if isinstance(button, dict) else {}
                    command = parse_command(action) if isinstance(action, dict) else None
                    if command is not None and command.project_key is not None:
                        project_key = command.project_key
                        break
                if project_key is not None:
                    break
            conversation_message_id = item.get("conversation_message_id")
            if project_key is None or conversation_message_id is None:
                continue
            try:
                await self.edit_text(
                    int(conversation_message_id),
                    str(item.get("text", "")),
                    keyboard=project_keyboard_json(project_key, decision_for(project_key)),
                )
            except VkApiError:
                # VK limits how long a sent message can be edited.
                LOGGER.debug("Не удалось обновить старую карточку %s", project_key, exc_info=True)

    async def send(
        self,
        project: Project,
        *,
        test_view: bool = False,
        decision: str | None = None,
        assessment: AiAssessment | None = None,
    ) -> None:
        await self.send_text(
            format_message(project, test_view=test_view, assessment=assessment),
            keyboard=project_keyboard_json(project.key, decision),
        )

    async def send_response_project(self, project: Project, *, client_replied: bool) -> None:
        status = "💬 Клиент написал" if client_replied else "⏳ Ждём ответа клиента"
        await self.send_text(
            f"{format_message(project)}\n\n{status}",
            keyboard=response_keyboard_json(project.key),
        )

    async def send_menu(self, message: str = "Выберите действие:") -> bool:
        return await self.send_text(message)

    async def _get_group_id(self) -> int:
        if self._group_id is not None:
            return self._group_id
        response = await self._api("groups.getById")
        groups = response.get("groups", []) if isinstance(response, dict) else response
        if not isinstance(groups, list) or not groups:
            raise RuntimeError("VK API не вернул ID сообщества")
        self._group_id = int(groups[0]["id"])
        return self._group_id

    async def _get_long_poll_server(self) -> dict[str, Any]:
        response = await self._api("groups.getLongPollServer", group_id=await self._get_group_id())
        if not isinstance(response, dict):
            raise RuntimeError("VK API не вернул Long Poll server")
        return response

    async def ensure_callback_events(self) -> None:
        """Enable the two Long Poll event types used by this bot."""
        try:
            await self._api(
                "groups.setLongPollSettings",
                group_id=await self._get_group_id(),
                enabled=1,
                api_version=self._api_version,
                message_new=1,
                message_event=1,
            )
        except VkApiError:
            LOGGER.warning(
                "Не удалось автоматически включить событие message_event в VK; "
                "проверьте настройки Long Poll сообщества",
                exc_info=True,
            )

    async def listen(
        self, handler: Callable[[BotCommand], Awaitable[None]], *, retry_seconds: int = 15
    ) -> None:
        """Listen for keyboard messages from the configured VK user."""
        await self.ensure_callback_events()
        while True:
            try:
                long_poll = await self._get_long_poll_server()
                server = str(long_poll["server"])
                key = str(long_poll["key"])
                ts = str(long_poll["ts"])
                LOGGER.info("VK Long Poll подключен; кнопки активны")
                while True:
                    async with self._session.get(
                        server,
                        params={"act": "a_check", "key": key, "ts": ts, "wait": 25},
                    ) as response:
                        response.raise_for_status()
                        data = await response.json()
                    if data.get("failed"):
                        if data["failed"] == 1:
                            ts = str(data["ts"])
                            continue
                        break
                    ts = str(data.get("ts", ts))
                    for update in data.get("updates", []):
                        update_type = update.get("type")
                        if update_type not in {"message_new", "message_event"}:
                            continue
                        update_object = update.get("object", {})
                        message = (
                            update_object.get("message", update_object)
                            if update_type == "message_new"
                            else update_object
                        )
                        if not isinstance(message, dict):
                            continue
                        sender_id = message.get(
                            "user_id" if update_type == "message_event" else "from_id",
                            0,
                        )
                        if int(sender_id) != self._user_id:
                            continue
                        command = parse_command(message)
                        if command is not None:
                            if update_type == "message_event":
                                try:
                                    await self.answer_event(command)
                                except VkApiError:
                                    LOGGER.debug(
                                        "Не удалось подтвердить callback VK", exc_info=True
                                    )
                            elif command.conversation_message_id is not None:
                                await self.delete_incoming(command.conversation_message_id)
                            await handler(command)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("VK Long Poll недоступен; повтор через %d секунд", retry_seconds)
                await asyncio.sleep(retry_seconds)


# Backwards-compatible name used by the polling code and older imports.
VkNotifier = VkBot
