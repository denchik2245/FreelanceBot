from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time

from freelance_bot.keywords import normalize_search_text
from freelance_bot.models import Project
from freelance_bot.storage import ProjectStore


@dataclass(frozen=True, slots=True)
class FilterSettings:
    min_budget: int
    min_ai_score: int
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    quiet_start: int | None
    quiet_end: int | None


def _keywords(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    result: list[str] = []
    for item in re.split(r"[,;\n]+", raw):
        normalized = normalize_search_text(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def project_budget(project: Project) -> int | None:
    values = [int(value.replace(" ", "")) for value in re.findall(r"\d[\d ]*", project.price)]
    return max(values) if values else None


def _contains(text: str, keyword: str) -> bool:
    return f" {keyword} " in f" {text} "


class ProjectFilterManager:
    def __init__(self, store: ProjectStore, *, default_ai_score: int) -> None:
        self._store = store
        self._default_ai_score = default_ai_score

    def settings(self) -> FilterSettings:
        quiet = self._store.get_state("filters:quiet_hours") or ""
        quiet_parts = quiet.split(":", maxsplit=1)
        quiet_start = int(quiet_parts[0]) if len(quiet_parts) == 2 else None
        quiet_end = int(quiet_parts[1]) if len(quiet_parts) == 2 else None
        return FilterSettings(
            min_budget=int(self._store.get_state("filters:min_budget") or 0),
            min_ai_score=int(
                self._store.get_state("filters:min_ai_score") or self._default_ai_score
            ),
            include_keywords=_keywords(self._store.get_state("filters:include_keywords")),
            exclude_keywords=_keywords(self._store.get_state("filters:exclude_keywords")),
            quiet_start=quiet_start,
            quiet_end=quiet_end,
        )

    def set_min_budget(self, value: int) -> None:
        if not 0 <= value <= 100_000_000:
            raise ValueError("Минимальный бюджет должен быть от 0 до 100 000 000 ₽")
        self._store.set_state("filters:min_budget", str(value))

    def set_min_ai_score(self, value: int) -> None:
        if not 0 <= value <= 100:
            raise ValueError("AI-балл должен быть от 0 до 100")
        self._store.set_state("filters:min_ai_score", str(value))

    def set_keywords(self, kind: str, value: str) -> None:
        if kind not in {"include", "exclude"}:
            raise ValueError("Неизвестный список слов")
        parsed = _keywords(value)
        if len(parsed) > 50:
            raise ValueError("Можно указать не больше 50 слов или фраз")
        if len("\n".join(parsed)) > 2_000:
            raise ValueError("Список слов слишком длинный")
        self._store.set_state(f"filters:{kind}_keywords", "\n".join(parsed))

    def set_quiet_hours(self, start: int | None, end: int | None) -> None:
        if start is None and end is None:
            self._store.set_state("filters:quiet_hours", "")
            return
        if start is None or end is None or not 0 <= start <= 23 or not 0 <= end <= 23:
            raise ValueError("Часы должны быть от 0 до 23")
        if start == end:
            raise ValueError("Начало и конец тихих часов должны отличаться")
        self._store.set_state("filters:quiet_hours", f"{start}:{end}")

    def rejection_reason(self, project: Project) -> str | None:
        settings = self.settings()
        text = normalize_search_text(
            f"{project.title} {project.description} {project.category}"
        )
        if any(_contains(text, keyword) for keyword in settings.exclude_keywords):
            return "исключающее слово"
        if settings.include_keywords and not any(
            _contains(text, keyword) for keyword in settings.include_keywords
        ):
            return "нет желательных слов"
        budget = project_budget(project)
        if budget is not None and budget < settings.min_budget:
            return f"бюджет {budget} ₽ ниже минимума"
        return None

    def is_quiet_now(self, current: datetime) -> bool:
        settings = self.settings()
        if settings.quiet_start is None or settings.quiet_end is None:
            return False
        current_time = current.timetz().replace(tzinfo=None)
        start = time(settings.quiet_start)
        end = time(settings.quiet_end)
        if start < end:
            return start <= current_time < end
        return current_time >= start or current_time < end
