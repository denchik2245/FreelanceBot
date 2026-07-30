from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from gigachat import GigaChat
from gigachat.models import Chat, JsonSchemaResponseFormat, Messages, MessagesRole

from freelance_bot.models import AiAssessment, Project

LOGGER = logging.getLogger(__name__)
MAX_PROJECT_TEXT_LENGTH = 6_000
RETRY_ATTEMPTS = 3
FILTER_RESPONSE_FORMAT = JsonSchemaResponseFormat(
    schema={
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["score", "reason"],
        "additionalProperties": False,
    },
    strict=True,
)


async def _with_retry[T](operation: Callable[[], Awaitable[T]], label: str) -> T:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await operation()
        except Exception:
            if attempt == RETRY_ATTEMPTS:
                raise
            delay = float(2 ** (attempt - 1))
            LOGGER.warning(
                "%s: GigaChat вернул некорректный ответ, повтор %d/%d через %.0f с",
                label,
                attempt + 1,
                RETRY_ATTEMPTS,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Недостижимая ветка повтора GigaChat")


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    messages = getattr(response, "messages", None)
    if not messages:
        raise RuntimeError("GigaChat вернул ответ без сообщений")
    content = getattr(messages[0], "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(part.text) for part in content if getattr(part, "text", None)]
        text = "".join(parts).strip()
        if text:
            return text
    raise RuntimeError("GigaChat вернул ответ без текста")


def _parse_filter_result(text: str) -> tuple[int, str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("GigaChat не вернул JSON с оценкой проекта")
    payload = json.loads(cleaned[start : end + 1])
    score = int(payload["score"])
    reason = " ".join(str(payload["reason"]).split())
    if not 0 <= score <= 100:
        raise ValueError("Оценка GigaChat должна быть от 0 до 100")
    if not reason:
        raise ValueError("GigaChat не объяснил оценку проекта")
    return score, reason


def _project_context(project: Project) -> str:
    # Kwork returns HTML fragments in descriptions. Tags and entities make the
    # project harder for the model to read and waste the limited input budget.
    description = re.sub(r"<[^>]+>", " ", project.description)
    description = " ".join(html.unescape(description).split())[:MAX_PROJECT_TEXT_LENGTH]
    return "\n".join(
        (
            f"Площадка: {project.source}",
            f"Название: {project.title}",
            f"Категория: {project.category}",
            f"Бюджет: {project.price or 'не указан'}",
            f"Описание: {description or 'не указано'}",
        )
    )


class GigaChatProjectAdvisor:
    def __init__(
        self,
        *,
        credentials: str,
        scope: str,
        base_url: str,
        ca_bundle_file: Path | None,
        filter_model: str,
        response_model: str,
        min_score: int,
        profile: str,
        filter_prompt: str,
        response_prompt: str,
    ) -> None:
        if not profile.strip():
            raise ValueError("Профиль исполнителя для GigaChat пуст")
        if not filter_prompt.strip():
            raise ValueError("Промпт для отбора проектов пуст")
        if not response_prompt.strip():
            raise ValueError("Промпт для написания отклика пуст")
        common: dict[str, Any] = {
            "credentials": credentials,
            "scope": scope,
            "base_url": base_url,
            "max_retries": 3,
            "retry_backoff_factor": 1.0,
        }
        if ca_bundle_file is not None:
            common["ca_bundle_file"] = str(ca_bundle_file)
        self._filter_client = GigaChat(model=filter_model, **common)
        self._response_client = GigaChat(model=response_model, **common)
        self._filter_model = filter_model
        self._response_model = response_model
        self._min_score = min_score
        self._profile = profile.strip()
        self._filter_prompt = filter_prompt.strip()
        self._response_prompt = response_prompt.strip()
        self._stack: AsyncExitStack | None = None

    @property
    def response_model(self) -> str:
        return self._response_model

    @classmethod
    def from_paths(
        cls,
        *,
        profile_path: Path,
        filter_prompt_path: Path,
        response_prompt_path: Path,
        **kwargs: Any,
    ) -> Self:
        def read_text(path: Path, description: str) -> str:
            try:
                return path.read_text(encoding="utf-8")
            except OSError as error:
                raise RuntimeError(f"Не удалось прочитать {description} {path}: {error}") from error

        return cls(
            profile=read_text(profile_path, "профиль исполнителя"),
            filter_prompt=read_text(filter_prompt_path, "промпт отбора проектов"),
            response_prompt=read_text(response_prompt_path, "промпт написания отклика"),
            **kwargs,
        )

    async def __aenter__(self) -> Self:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            await stack.enter_async_context(self._filter_client)
            await stack.enter_async_context(self._response_client)
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc_value, traceback)
            self._stack = None

    async def assess(self, project: Project) -> AiAssessment:
        filter_request = Chat(
            model=self._filter_model,
            temperature=0.1,
            max_tokens=400,
            response_format=FILTER_RESPONSE_FORMAT,
            messages=[
                Messages(
                    role=MessagesRole.SYSTEM,
                    content=(f"{self._filter_prompt}\n\nПРОФИЛЬ ИСПОЛНИТЕЛЯ:\n{self._profile}"),
                ),
                Messages(role=MessagesRole.USER, content=_project_context(project)),
            ],
        )

        async def request_filter(client: GigaChat, requested_model: str) -> tuple[int, str, str]:
            # Keep the model in the payload as well as in the client settings so
            # SDK defaults can never silently route the request to another tier.
            request = filter_request.model_copy(update={"model": requested_model}, deep=True)
            response = await client.achat(request)
            score, reason = _parse_filter_result(_response_text(response))
            actual_model = str(getattr(response, "model", "") or requested_model)
            return score, reason, actual_model

        try:
            score, reason, used_model = await _with_retry(
                lambda: request_filter(self._filter_client, self._filter_model),
                f"AI-оценка {project.key}",
            )
        except Exception:
            if self._response_model == self._filter_model:
                raise
            LOGGER.warning(
                "Модель оценки %s не оценила %s; пробую резервную %s",
                self._filter_model,
                project.key,
                self._response_model,
                exc_info=True,
            )
            score, reason, used_model = await _with_retry(
                lambda: request_filter(self._response_client, self._response_model),
                f"резервная AI-оценка {project.key}",
            )
        suitable = score >= self._min_score
        assessment = AiAssessment(
            project_key=project.key,
            suitable=suitable,
            score=score,
            reason=reason,
            response_text="",
            filter_model=used_model,
            response_model="",
        )
        LOGGER.info(
            "AI-оценка %s моделью %s: %d/100, подходит=%s — %s",
            project.key,
            used_model,
            score,
            suitable,
            reason,
        )
        return assessment

    async def generate_response(self, project: Project, *, previous_response: str = "") -> str:
        system_content = f"{self._response_prompt}\n\nПРОФИЛЬ ИСПОЛНИТЕЛЯ:\n{self._profile}"
        user_content = _project_context(project)
        if previous_response:
            system_content += (
                "\n\nЭто повторная генерация. Предыдущий отклик дан только для сравнения. "
                "Создай заметно другой вариант: измени формулировки и хотя бы один из акцентов, "
                "идей или следующих шагов. Не комментируй различия."
            )
            user_content += f"\n\nПРЕДЫДУЩИЙ ОТКЛИК:\n{previous_response}"
        response_request = Chat(
            model=self._response_model,
            temperature=0.5,
            max_tokens=900,
            messages=[
                Messages(
                    role=MessagesRole.SYSTEM,
                    content=system_content,
                ),
                Messages(role=MessagesRole.USER, content=user_content),
            ],
        )

        async def request_response() -> str:
            response = await self._response_client.achat(response_request)
            actual_model = str(getattr(response, "model", "") or self._response_model)
            LOGGER.info("AI-отклик %s сгенерирован моделью %s", project.key, actual_model)
            return _response_text(response)

        return await _with_retry(request_response, f"AI-отклик {project.key}")
