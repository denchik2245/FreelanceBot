"""Validated portfolio facts; selection does not infer unrecorded experience."""

import json
import re
from urllib.parse import urlsplit

from freelance_bot.models import Project


def parse_portfolio(text: str) -> list[dict[str, str]]:
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Портфолио должно быть JSON-массивом")  # noqa: TRY004
    fields = {"id", "name", "product", "work", "platform", "url"}
    ids: set[str] = set()
    for case in data:
        if not isinstance(case, dict) or set(case) != fields:
            raise ValueError("Кейс: нужны id, name, product, work, platform, url")
        if any(not isinstance(v, str) or not v.strip() for v in case.values()):
            raise ValueError("Поля кейса должны быть непустыми строками")
        if case["id"] in ids:
            raise ValueError("ID кейсов должны быть уникальными")
        ids.add(case["id"])
        url = urlsplit(case["url"])
        if url.scheme != "https" or not url.netloc or re.search(r"\s", case["url"]):
            raise ValueError("Ссылка кейса должна быть абсолютным HTTPS URL")
    return data


def select_cases(project: Project, cases: list[dict[str, str]]) -> list[dict[str, str]]:
    """Rank a small factual shortlist, leaving final relevance judgement to the writer."""
    text = f"{project.title} {project.description}".casefold()
    tilda = bool(re.search(r"tilda|тильд", text))
    def kind(value: str) -> str:
        if re.search(r"приложени|веб-сервис|кабинет|интерфейс", value):
            return "interface"
        if re.search(r"магазин|каталог|карточк.{0,10}товар", value):
            return "store"
        if re.search(r"лендинг|посадоч", value):
            return "landing"
        if re.search(r"сайт|страниц", value):
            return "website"
        return "unknown"

    target_kind = kind(text)
    ignored = {"компа", "сайта", "сайто", "ленди", "дизай", "макет", "figma", "проек",
               "страни", "нужен", "нужны", "корпо", "много", "магаз", "работ"}
    tokens = {word[:5] for word in re.findall(r"[а-яa-z]{5,}", text)} - ignored

    def rank(case: dict[str, str]) -> int:
        facts = f"{case['name']} {case['product']}".casefold()
        case_kind = kind(facts)
        # Generic Figma experience cannot prove mobile-app or e-commerce experience.
        if target_kind in {"interface", "store"} and case_kind != target_kind:
            return 0
        overlap = len(tokens & {word[:5] for word in re.findall(r"[а-яa-z]{5,}", facts)})
        same_kind = target_kind != "unknown" and case_kind == target_kind
        return (overlap * 10 + (5 if same_kind else 0)
                + (30 if tilda and case["platform"] == "Tilda" else 0))

    # No arbitrary fallback links. Lexical matches are candidates, not proof of niche experience.
    ranked = sorted(cases, key=rank, reverse=True)
    return [case for case in ranked if rank(case) > 0][:6]


def response_issues(text: str, allowed_urls: set[str], finish_reason: object) -> list[str]:
    issues = []
    if not text.strip():
        issues.append("Ответ пустой")
    if str(getattr(finish_reason, "value", finish_reason)) in {"length", "max_tokens"}:
        issues.append("Ответ обрезан лимитом генерации; сократи и заверши сообщение")
    urls = re.findall(
        r"https?://[^\s<>]+|\b(?:[a-z0-9-]+\.)+[a-z]{2,24}(?:/[^\s<>]*)?",
        text, flags=re.IGNORECASE,
    )
    if any(url.rstrip(".,;!?)\"]}") not in allowed_urls for url in urls):
        issues.append("Используй только точные разрешённые ссылки или убери ссылки")
    if re.search(
        r"(?:изучил|посмотрел|проанализировал|ознакомился).{0,35}"
        r"(?:сайт|макет|файл|текущ.{0,10}верси|ссылк)", text, re.IGNORECASE,
    ):
        issues.append("Содержимое ссылок не передано: убери утверждение, что уже изучил материалы")
    if text.count("?") > 1:
        issues.append("Оставь не больше одного уточняющего вопроса")
    if re.search(
        r"успешн\w*\s+(?:проект|опыт)|недавно.{0,25}(?:проект|заверш)|"
        r"(?:завершал|выполнял).{0,25}проект|"
        r"(?:есть|имею|имеется|обладаю).{0,12}опыт|"
        r"(?:приступить|начать|сделаю|завершу).{0,20}(?:сегодня|завтра)",
        text, re.IGNORECASE,
    ):
        issues.append("Убери неподтверждённые успехи, недавние работы и обещание начать сегодня/завтра")
    return issues


def requested_case_count(project: Project) -> int | None:
    text = f"{project.title} {project.description}".casefold()
    if re.search(r"без портфолио|не (?:присылайте|добавляйте) (?:портфолио|примеры)", text):
        return 0
    match = re.search(r"\b(один|одного|два|двух|три|трёх|трех|[1-6])\s+пример", text)
    if not match:
        return None
    numbers = {"один": 1, "одного": 1, "два": 2, "двух": 2,
               "три": 3, "трёх": 3, "трех": 3}
    return numbers.get(match[1], int(match[1]) if match[1].isdigit() else 0)
