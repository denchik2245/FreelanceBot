from __future__ import annotations

import re
from dataclasses import dataclass

from freelance_bot.models import Project
from freelance_bot.storage import ProjectStore


@dataclass(frozen=True, slots=True)
class FilterSettings:
    min_budget: int
    min_ai_score: int


def project_budget(project: Project) -> int | None:
    values = [int(value.replace(" ", "")) for value in re.findall(r"\d[\d ]*", project.price)]
    return max(values) if values else None


class ProjectFilterManager:
    def __init__(self, store: ProjectStore, *, default_ai_score: int) -> None:
        self._store = store
        self._default_ai_score = default_ai_score

    def settings(self) -> FilterSettings:
        return FilterSettings(
            min_budget=int(self._store.get_state("filters:min_budget") or 0),
            min_ai_score=int(
                self._store.get_state("filters:min_ai_score") or self._default_ai_score
            ),
        )

    def set_min_budget(self, value: int) -> None:
        if not 0 <= value <= 100_000_000:
            raise ValueError("Минимальный бюджет должен быть от 0 до 100 000 000 ₽")
        self._store.set_state("filters:min_budget", str(value))

    def set_min_ai_score(self, value: int) -> None:
        if not 0 <= value <= 100:
            raise ValueError("AI-балл должен быть от 0 до 100")
        self._store.set_state("filters:min_ai_score", str(value))

    def rejection_reason(self, project: Project) -> str | None:
        settings = self.settings()
        budget = project_budget(project)
        if budget is not None and budget < settings.min_budget:
            return f"бюджет {budget} ₽ ниже минимума"
        return None
