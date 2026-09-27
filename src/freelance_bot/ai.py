from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from gigachat import GigaChat
from gigachat.models import Chat, JsonSchemaResponseFormat, Messages, MessagesRole

from freelance_bot.models import AiAssessment, Project
from freelance_bot.portfolio import (
    load_portfolio_documents,
    parse_portfolio,
    requested_case_count,
    response_issues,
    select_cases,
)

LOGGER = logging.getLogger(__name__)
MAX_PROJECT_TEXT_LENGTH = 6_000
RETRY_ATTEMPTS = 3


class InsufficientPortfolioCasesError(ValueError):
    """The client's explicit request exceeds the available relevant portfolio cases."""


FILTER_RESPONSE_FORMAT = JsonSchemaResponseFormat(
    schema={
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["accept", "reject", "unclear"]},
            "reason": {"type": "string", "minLength": 1},
            "summary": {"type": "string"},
        },
        "required": ["decision", "reason", "summary"],
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


def _parse_filter_result(text: str) -> tuple[str, str, str]:
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
    fields = {"decision", "reason", "summary"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("Нужны decision, reason, summary")
    if any(not isinstance(value, str) for value in payload.values()):
        raise ValueError("Поля решения должны быть строками")
    decision, reason, summary = (
        " ".join(payload[key].split())
        for key in ("decision", "reason", "summary")
    )
    if decision not in {"accept", "reject", "unclear"}:
        raise ValueError("Неизвестное решение отбора")
    if not reason:
        raise ValueError("GigaChat не объяснил оценку проекта")
    if decision == "accept" and not summary:
        raise ValueError("GigaChat не описал задачу клиента")
    return decision, reason, summary if decision == "accept" else ""


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
        filter_fallback_model: str,
        response_model: str,
        min_score: int,
        profile: str,
        filter_prompt: str,
        response_prompt: str,
        portfolio: str = "[]",
        portfolio_documents: dict[str, dict[str, str]] | None = None,
        verify_accepted: bool = True,
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
        self._filter_fallback_client = GigaChat(model=filter_fallback_model, **common)
        self._response_client = GigaChat(model=response_model, **common)
        self._filter_model = filter_model
        self._filter_fallback_model = filter_fallback_model
        self._response_model = response_model
        self._min_score = min_score
        self._profile = profile.strip()
        self._filter_prompt = filter_prompt.strip()
        self._response_prompt = response_prompt.strip()
        self._portfolio = parse_portfolio(portfolio)
        self._portfolio_documents = portfolio_documents or {}
        if portfolio_documents is not None and {
            case["id"]: case["url"] for case in self._portfolio
        } != {
            case_id: document["url"]
            for case_id, document in self._portfolio_documents.items()
        }:
            raise ValueError("Карточки портфолио не совпадают с JSON-каталогом")
        self._verify_accepted = verify_accepted
        self._stack: AsyncExitStack | None = None

    @property
    def response_model(self) -> str:
        return self._response_model

    @property
    def filter_revision(self) -> str:
        """Identify the rules used to assess a project, including live config edits."""
        rules = json.dumps(
            ["decision-v6", self._filter_prompt, self._filter_model,
             getattr(self, "_filter_fallback_model", self._response_model),
             getattr(self, "_verify_accepted", False)],
            ensure_ascii=False,
        )
        return sha256(rules.encode("utf-8")).hexdigest()

    def update_config_text(self, key: str, text: str) -> None:
        """Apply an already validated config edit without restarting the clients."""
        value = text.strip()
        if not value:
            raise ValueError("AI-текст не может быть пустым")
        if key == "portfolio":
            updated = parse_portfolio(value)
            documents = getattr(self, "_portfolio_documents", {})
            if documents and {case["id"]: case["url"] for case in updated} != {
                case_id: document["url"] for case_id, document in documents.items()
            }:
                raise ValueError(
                    "Набор кейсов и ссылки не совпадают с Markdown-карточками. "
                    "Обновите config/portfolio-index.md и config/portfolio-cases на сервере."
                )
            self._portfolio = updated
            return
        attributes = {
            "profile": "_profile",
            "filter": "_filter_prompt",
            "response": "_response_prompt",
        }
        try:
            attribute = attributes[key]
        except KeyError as error:
            raise ValueError("Неизвестный AI-текст") from error
        setattr(self, attribute, value)

    def set_min_score(self, value: int) -> None:
        if not 0 <= value <= 100:
            raise ValueError("AI-балл должен быть от 0 до 100")
        self._min_score = value

    @classmethod
    def from_paths(
        cls,
        *,
        profile_path: Path,
        filter_prompt_path: Path,
        response_prompt_path: Path,
        portfolio_path: Path | None = None,
        **kwargs: Any,
    ) -> Self:
        def read_text(path: Path, description: str) -> str:
            try:
                return path.read_text(encoding="utf-8")
            except OSError as error:
                raise RuntimeError(f"Не удалось прочитать {description} {path}: {error}") from error

        portfolio_text = read_text(portfolio_path, "портфолио") if portfolio_path else "[]"
        documents = (
            load_portfolio_documents(portfolio_path, parse_portfolio(portfolio_text))
            if portfolio_path else None
        )
        return cls(
            profile=read_text(profile_path, "профиль исполнителя"),
            filter_prompt=read_text(filter_prompt_path, "промпт отбора проектов"),
            response_prompt=read_text(response_prompt_path, "промпт написания отклика"),
            portfolio=portfolio_text,
            portfolio_documents=documents,
            **kwargs,
        )

    async def __aenter__(self) -> Self:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            await stack.enter_async_context(self._filter_client)
            await stack.enter_async_context(self._filter_fallback_client)
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
        # Capture before awaiting: a VK config edit may arrive during this request.
        filter_revision = self.filter_revision
        filter_request = Chat(
            model=self._filter_model,
            temperature=0.1,
            max_tokens=500,
            response_format=FILTER_RESPONSE_FORMAT,
            messages=[
                Messages(
                    role=MessagesRole.SYSTEM,
                    content=self._filter_prompt,
                ),
                Messages(role=MessagesRole.USER, content=_project_context(project)),
            ],
        )

        async def request_filter(
            client: GigaChat, requested_model: str
        ) -> tuple[str, str, str, str]:
            # Keep the model in the payload as well as in the client settings so
            # SDK defaults can never silently route the request to another tier.
            request = filter_request.model_copy(update={"model": requested_model}, deep=True)
            response = await client.achat(request)
            decision, reason, summary = _parse_filter_result(_response_text(response))
            actual_model = str(getattr(response, "model", "") or requested_model)
            return decision, reason, summary, actual_model

        fallback_model = getattr(self, "_filter_fallback_model", self._response_model)
        fallback_client = getattr(self, "_filter_fallback_client", self._response_client)
        used_fallback = False
        try:
            decision, reason, summary, used_model = await _with_retry(
                lambda: request_filter(self._filter_client, self._filter_model),
                f"AI-оценка {project.key}",
            )
        except Exception:
            if fallback_model == self._filter_model:
                raise
            used_fallback = True
            LOGGER.warning(
                "Модель оценки %s не оценила %s; пробую резервную %s",
                self._filter_model,
                project.key,
                fallback_model,
                exc_info=True,
            )
            decision, reason, summary, used_model = await _with_retry(
                lambda: request_filter(fallback_client, fallback_model),
                f"резервная AI-оценка {project.key}",
            )
        source_text = " ".join(html.unescape(re.sub(
            r"<[^>]+>", " ", project.description
        )).split())
        if (decision == "accept" and source_text
                and len(source_text) <= MAX_PROJECT_TEXT_LENGTH
                and getattr(self, "_verify_accepted", False) and not used_fallback
                and fallback_model != self._filter_model):
            # Independent decision: do not anchor the verifier with Pro's explanation.
            decision, reason, summary, used_model = await _with_retry(
                lambda: request_filter(fallback_client, fallback_model),
                f"проверка кандидата {project.key}",
            )
        if decision == "accept" and (
            not source_text or len(source_text) > MAX_PROJECT_TEXT_LENGTH
        ):
            decision, summary = "unclear", ""
            reason = "Описание отсутствует или обрезано"
        suitable = decision == "accept"
        score = 100 if suitable else 0  # Compatibility with historical database rows only.
        assessment = AiAssessment(
            project_key=project.key,
            suitable=suitable,
            score=score,
            reason=reason,
            response_text="",
            filter_model=used_model,
            response_model="",
            summary=summary,
            filter_revision=filter_revision,
            decision=decision,
            evidence="",
        )
        LOGGER.info(
            "AI-отбор %s моделью %s: %s, подходит=%s — %s",
            project.key,
            used_model,
            decision,
            suitable,
            reason,
        )
        return assessment

    async def generate_response(
        self,
        project: Project,
        *,
        previous_response: str = "",
        variation: str | None = None,
    ) -> str:
        documents = getattr(self, "_portfolio_documents", {})
        index_terms = {
            case_id: document["search_text"] for case_id, document in documents.items()
        }
        cases = select_cases(project, getattr(self, "_portfolio", []), index_terms)
        requested_count = requested_case_count(project)
        if requested_count == 0:
            cases = []
        if requested_count is not None and requested_count > len(cases):
            raise InsufficientPortfolioCasesError(
                f"Заказчик просит {requested_count} примера работ, "
                f"а подходящих подтверждённых кейсов найдено {len(cases)}. "
                "Добавьте кейсы в портфолио или подготовьте отклик вручную."
            )
        # Full cards are substantial; keep a small shortlist unless the client asks for more.
        cases = cases[:max(3, requested_count or 0)]
        required_count = requested_count if requested_count is not None else 0
        maximum_count = required_count if requested_count is not None else min(2, len(cases))
        case_facts = []
        for case in cases:
            facts = {k: v for k, v in case.items() if k != "url"}
            if case["id"] in documents:
                facts["verified_card"] = documents[case["id"]]["card"]
            case_facts.append(facts)
        system_content = (
            f"{self._response_prompt}\n\nФАКТЫ ОБ ИСПОЛНИТЕЛЕ:\n{self._profile}"
            f"\n\nКЕЙСЫ-КАНДИДАТЫ (выбери уместные, можно не использовать):\n"
            f"{json.dumps(case_facts, ensure_ascii=False)}"
        )
        if not cases:
            system_content += (
                "\nПодходящих кейсов в переданных данных нет. Не заявляй об аналогичном опыте. "
                "Если клиент спросил о нём, честно сообщи, что не можешь привести аналогичный кейс."
            )
        user_content = _project_context(project)
        if previous_response:
            instructions = {
                None: (
                    "Создай заметно другой вариант: измени формулировки и хотя бы один из "
                    "акцентов, идей или следующих шагов."
                ),
                "different": (
                    "Создай заметно другой вариант: измени формулировки и хотя бы один из "
                    "акцентов, идей или следующих шагов."
                ),
                "shorter": (
                    "Перепиши отклик заметно короче. Сохрани главные аргументы и конкретику, "
                    "убери повторы и второстепенные детали."
                ),
                "formal": (
                    "Перепиши отклик в более деловом, уверенном и сдержанном стиле без канцелярита."
                ),
                "friendly": (
                    "Перепиши отклик более живо, тепло и естественно, но без фамильярности."
                ),
                "question": (
                    "Улучши отклик: оставь или замени существующий вопрос на один уместный, "
                    "который поможет уточнить задачу и начать диалог."
                ),
            }
            try:
                revision_instruction = instructions[variation]
            except KeyError as error:
                raise ValueError("Неизвестный вариант изменения отклика") from error
            system_content += (
                "\n\nЭто повторная генерация. Предыдущий отклик дан как основа для правки. "
                "Сохрани подтверждённые факты, ответы на вопросы клиента и уместные кейсы. "
                "Предыдущий текст не является источником фактов. "
                f"{revision_instruction} Не комментируй изменения и верни только готовый отклик "
                "в JSON с полями body и case_ids."
            )
            user_content += f"\n\nПРЕДЫДУЩИЙ ОТКЛИК:\n{previous_response}"
        elif variation is not None:
            raise ValueError("Нельзя изменить ещё не созданный отклик")
        response_request = Chat(
            model=self._response_model,
            temperature=0.3,
            max_tokens=900,
            response_format=JsonSchemaResponseFormat(schema={
                "type": "object",
                "properties": {
                    "body": {"type": "string", "minLength": 1},
                    "case_ids": {
                        "type": "array", "minItems": required_count,
                        "maxItems": maximum_count, "uniqueItems": True,
                        "items": ({"type": "string", "enum": [c["id"] for c in cases]}
                                  if cases else {"type": "string"}),
                    },
                },
                "required": ["body", "case_ids"], "additionalProperties": False,
            }, strict=True),
            messages=[
                Messages(
                    role=MessagesRole.SYSTEM,
                    content=system_content,
                ),
                Messages(role=MessagesRole.USER, content=user_content),
            ],
        )

        async def request_response() -> Any:
            response = await self._response_client.achat(response_request)
            actual_model = str(getattr(response, "model", "") or self._response_model)
            LOGGER.info("AI-отклик %s сгенерирован моделью %s", project.key, actual_model)
            return response

        case_map = {case["id"]: case for case in cases}
        for attempt in range(2):
            response = await _with_retry(request_response, f"AI-отклик {project.key}")
            try:
                text = _response_text(response)
            except RuntimeError:
                text = ""
            choices = getattr(response, "choices", [])
            finish = getattr(choices[0], "finish_reason", None) if choices else None
            try:
                payload = json.loads(text)
                if (not isinstance(payload, dict) or set(payload) != {"body", "case_ids"}
                        or not isinstance(payload["body"], str)
                        or not isinstance(payload["case_ids"], list)
                        or any(not isinstance(key, str) or key not in case_map
                               for key in payload["case_ids"])):
                    raise ValueError("Некорректный формат отклика или неизвестный ID кейса")
                body = payload["body"].strip()
                selected = list(dict.fromkeys(payload["case_ids"]))
                issues = response_issues(body, set(), finish)
                if (len(re.findall(r"[а-яё]", user_content, re.IGNORECASE)) >= 20
                        and len(re.findall(r"[а-яё]", body, re.IGNORECASE)) < 10
                        and not re.search(
                            r"на английском|по-английски|in english", user_content, re.IGNORECASE
                        )):
                    issues.append("Пиши на русском; английское кодовое слово не меняет язык отклика")
                if not required_count <= len(selected) <= maximum_count:
                    issues.append(
                        f"В case_ids должно быть от {required_count} до {maximum_count} "
                        "уникальных разрешённых ID; это требование клиента к примерам"
                    )
                markers = re.findall(r"\{\{CASE:([a-z0-9-]+)\}\}", body)
                if markers != selected:
                    issues.append(
                        "Для каждого ID из case_ids вставь в body ровно один маркер "
                        "{{CASE:id}} в том же порядке; лишние маркеры запрещены"
                    )
                body_without_markers = re.sub(r"\{\{CASE:[a-z0-9-]+\}\}", "", body)
                if any(key in body_without_markers for key in case_map):
                    issues.append("Убери служебные ID вне маркеров {{CASE:id}}")
            except (ValueError, TypeError):
                issues = ["Верни JSON с body (текст без ссылок) и case_ids (массив разрешённых ID)"]
            if not issues:
                # The model chooses placement and explains relevance; the application inserts
                # only catalogued facts and exact URLs so it cannot invent portfolio evidence.
                for key in selected:
                    case = case_map[key]
                    replacement = f"{case['name']}: {case['url']}\n{case['work']}"
                    body = body.replace(f"{{{{CASE:{key}}}}}", replacement)
                return body
            if attempt == 0:
                response_request.messages.append(Messages(
                    role=MessagesRole.ASSISTANT, content=text or "(пустой ответ)"
                ))
                response_request.messages.append(Messages(
                    role=MessagesRole.USER,
                    content="Исправь отклик: " + "; ".join(issues)
                    + ". Верни только полный исправленный JSON с полями body и case_ids.",
                ))
        raise ValueError("Отклик не прошёл проверку: " + "; ".join(issues))
