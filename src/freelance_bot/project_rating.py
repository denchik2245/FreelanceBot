"""Advisory A/Б/В labels; these rules never control notification delivery."""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from freelance_bot.models import AiAssessment, Project
from freelance_bot.project_filters import project_budget

# The analysis calls this a "large history" without an exact threshold.
# Ten published projects is the explicit operational interpretation.
LARGE_CLIENT_HISTORY = 10


@dataclass(frozen=True, slots=True)
class ProjectRating:
    level: str
    reason: str


def rate_project(
    project: Project, assessment: AiAssessment | None = None, *, now: datetime | None = None,
) -> ProjectRating:
    normalized_price = project.price.replace("\u00a0", " ").replace("\u202f", " ")
    # The source filters retain their current behavior. Only the advisory label
    # refuses to compare foreign currency or hourly rates with a ruble project fee.
    non_project_price = re.search(
        r"[$€£]|\b(?:USD|EUR|доллар\w*|евро)\b|(?:/|за\s+)\s*(?:час|ч\b|часов)",
        normalized_price, re.IGNORECASE,
    )
    budget = None if non_project_price else project_budget(replace(project, price=normalized_price))
    offers = project.offers_count
    history = project.client_projects_count
    hired = project.client_hired_percent
    clear = assessment.requirements_clear if assessment else None

    if budget is not None and budget < 3_000:
        return ProjectRating("В", "бюджет ниже 3 000 ₽")
    if hired is not None and history is not None:
        if hired == 0 and history >= 5:
            return ProjectRating("В", f"найм 0% при {history} проектах заказчика")
        if hired < 30 and history >= LARGE_CLIENT_HISTORY:
            return ProjectRating("В", f"найм {hired:g}% при {history} проектах заказчика")
    if project.published_at is not None:
        published = project.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        if (now or datetime.now(UTC)) - published > timedelta(days=14):
            return ProjectRating("В", "проект опубликован больше двух недель назад")

    # CONVERSION-PLAN.md corrects the original crowd rule using the temyroff case.
    # This exception overrides crowd size, never known poor hiring history.
    if budget is not None and budget >= 30_000 and clear is True:
        return ProjectRating("А", "бюджет от 30 000 ₽ и подробное ТЗ")
    if offers is not None and offers >= 150 and history is not None and history < 5:
        return ProjectRating("В", f"{offers} откликов у нового заказчика")
    if budget is not None and budget >= 15_000 and clear is True:
        if hired is not None and hired >= 50:
            return ProjectRating("А", f"бюджет от 15 000 ₽, понятное ТЗ, найм {hired:g}%")
        if offers is not None and offers < 40:
            return ProjectRating("А", f"бюджет от 15 000 ₽, понятное ТЗ, откликов {offers}")

    missing = []
    if budget is None:
        missing.append("бюджет")
    if clear is None:
        missing.append("ясность ТЗ")
    if hired is None and offers is None:
        missing.append("найм и число откликов")
    if missing:
        return ProjectRating("Б", "предварительно: неизвестны " + ", ".join(missing))
    if clear is False:
        return ProjectRating("Б", "ТЗ требует уточнения")
    if offers is not None and 40 <= offers <= 120:
        return ProjectRating("Б", f"откликов {offers}; нет всех условий уровня А")
    return ProjectRating("Б", "нет всех условий уровня А и явных признаков уровня В")
