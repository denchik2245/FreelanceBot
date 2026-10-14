"""Direct DeepSeek transport for the advisor's existing chat requests."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Self

import aiohttp
from gigachat.models import Chat

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplyMessage:
    content: str


@dataclass(frozen=True)
class ReplyChoice:
    message: ReplyMessage
    finish_reason: str


@dataclass(frozen=True)
class Reply:
    model: str
    choices: list[ReplyChoice]


class DeepSeekApiError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        hints = {
            401: "проверьте DEEPSEEK_API_KEY",
            402: "пополните баланс DeepSeek API",
            429: "превышен лимит запросов",
        }
        super().__init__(f"DeepSeek API: HTTP {status}; {hints.get(status, 'запрос не выполнен')}")


class DeepSeekClient:
    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Self:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60, connect=15),
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def achat(self, request: Chat) -> Reply:
        if self._session is None:
            raise RuntimeError("Клиент DeepSeek не открыт")
        original = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        payload = {
            key: original[key]
            for key in ("model", "messages", "temperature", "max_tokens")
            if key in original
        }
        payload["stream"] = False
        payload["thinking"] = {"type": "disabled"}
        # DeepSeek JSON mode guarantees an object, not our schema. The advisor
        # still validates decisions, required fields, case IDs and response text.
        response_format = original.get("response_format")
        if response_format:
            payload["response_format"] = {"type": "json_object"}
            schema = response_format.get("schema", {})
            instruction = (
                "Верни только JSON-объект без Markdown, соответствующий этой схеме: "
                + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            )
            payload["messages"] = [
                {"role": "system", "content": instruction},
                *payload["messages"],
            ]
        try:
            async with self._session.post(
                f"{self._base_url}/chat/completions", json=payload, allow_redirects=False,
            ) as response:
                if response.status != 200:
                    # Never propagate response bodies or headers into bot logs.
                    raise DeepSeekApiError(response.status)
                try:
                    data = await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    raise RuntimeError("DeepSeek вернул некорректный JSON ответа API") from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise RuntimeError("Не удалось соединиться с DeepSeek API") from None

        if not isinstance(data, dict):
            raise RuntimeError("DeepSeek вернул некорректный ответ API")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise RuntimeError("DeepSeek вернул ответ без сообщений")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason not in {"stop", "length"}:
            raise RuntimeError("DeepSeek не завершил генерацию ответа")
        message = choice.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise RuntimeError("DeepSeek вернул ответ без текста")
        model = data.get("model")
        usage = data.get("usage")
        if isinstance(usage, dict):
            tokens = [usage.get(key, 0) for key in (
                "prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens",
            )]
            if all(type(value) is int and value >= 0 for value in tokens):
                LOGGER.info(
                    "DeepSeek токены: вход=%d, выход=%d, кешированный вход=%d", *tokens,
                )
        return Reply(
            model=model if isinstance(model, str) else str(payload["model"]),
            choices=[ReplyChoice(
                message=ReplyMessage(content=message["content"]),
                finish_reason=finish_reason,
            )],
        )
