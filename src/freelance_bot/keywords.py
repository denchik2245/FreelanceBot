import re
import unicodedata

from freelance_bot.models import Project

# The first-stage filter is deliberately broad: false positives are cheaper than
# false negatives because GigaChat performs the final semantic assessment.
PROJECT_KEYWORDS: tuple[str, ...] = (
    "сайт",
    "веб",
    "web",
    "лендинг",
    "лэндинг",
    "landing",
    "интернет магазин",
    "online store",
    "ecommerce",
    "e commerce",
    "дизайн",
    "design",
    "редизайн",
    "интерфейс",
    "ui",
    "ux",
    "figma",
    "tilda",
    "тильда",
    "zero block",
    "зеро блок",
    "прототип",
    "макет",
)
PREFIX_KEYWORDS = frozenset(
    {"сайт", "веб", "лендинг", "лэндинг", "дизайн", "редизайн", "интерфейс", "прототип", "макет"}
)


def normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^a-zа-я0-9]+", " ", normalized).split())


def matching_project_keywords(project: Project) -> tuple[str, ...]:
    text = normalize_search_text(f"{project.title} {project.description} {project.category}")
    padded = f" {text} "
    tokens = text.split()
    return tuple(
        keyword
        for keyword in PROJECT_KEYWORDS
        if (
            any(token.startswith(keyword) for token in tokens)
            if keyword in PREFIX_KEYWORDS
            else f" {keyword} " in padded
        )
    )


def matches_project_keywords(project: Project) -> bool:
    return bool(matching_project_keywords(project))
