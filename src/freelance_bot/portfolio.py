"""Validated portfolio facts; selection does not infer unrecorded experience."""

import json
import re
from pathlib import Path
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
        if re.fullmatch(r"[a-z0-9-]+", case["id"]) is None:
            raise ValueError("ID кейса может содержать только a-z, 0-9 и дефис")
        if case["id"] in ids:
            raise ValueError("ID кейсов должны быть уникальными")
        ids.add(case["id"])
        url = urlsplit(case["url"])
        if url.scheme != "https" or not url.netloc or re.search(r"\s", case["url"]):
            raise ValueError("Ссылка кейса должна быть абсолютным HTTPS URL")
    return data


def load_portfolio_documents(
    portfolio_path: Path, cases: list[dict[str, str]]
) -> dict[str, dict[str, str]]:
    """Load the skill index and verified case cards alongside portfolio.json."""
    index_path = portfolio_path.with_name("portfolio-index.md")
    cards_dir = portfolio_path.with_name("portfolio-cases")
    try:
        index = index_path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"Не удалось прочитать индекс портфолио {index_path}") from error

    rows: dict[str, tuple[str, str]] = {}
    for line in index.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6 or not re.fullmatch(r"`[a-z0-9-]+`", cells[0]):
            continue
        case_id = cells[0].strip("`")
        card_link = re.fullmatch(r"\[Открыть\]\(cases/([a-z0-9-]+)\.md\)", cells[5])
        if not card_link or card_link[1] != case_id or case_id in rows:
            raise ValueError(f"Некорректная строка индекса для кейса {case_id}")
        rows[case_id] = (" ".join(cells[1:4]), cells[4])

    catalog = {case["id"]: case for case in cases}
    if rows.keys() != catalog.keys():
        raise ValueError("ID в portfolio-index.md и portfolio.json должны совпадать")

    documents: dict[str, dict[str, str]] = {}
    for case_id, case in catalog.items():
        card_path = cards_dir / f"{case_id}.md"
        try:
            card = card_path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(f"Не удалось прочитать карточку кейса {card_path}") from error
        card_id = re.search(r"(?m)^- ID: `([a-z0-9-]+)`\s*$", card)
        card_url = re.search(r"(?m)^- Публичная работа: (https://\S+)\s*$", card)
        if not card_id or card_id[1] != case_id or not card_url or card_url[1] != case["url"]:
            raise ValueError(f"ID или публичная ссылка в карточке {case_id} не совпадает с JSON")
        # The application inserts the verified URL; the model should see facts, not links.
        safe_card = re.sub(
            r"(?m)^- (?:Публичная работа|Сайт из кейса):[^\n]*\n?", "", card
        ).strip()
        documents[case_id] = {
            "search_text": rows[case_id][0],
            "card": safe_card,
            "url": case["url"],
        }
    return documents


def select_cases(
    project: Project,
    cases: list[dict[str, str]],
    index_terms: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Rank a small factual shortlist, leaving final relevance judgement to the writer."""
    text = f"{project.title} {project.description}".casefold()
    tilda_mentioned = bool(re.search(r"tilda|тильд", text))
    tilda_ruled_out = bool(re.search(
        r"(?:tilda|тильд\w*)(?:\s+\w+){0,2}\s+не\s+"
        r"(?:подход|нужн|использ|выбран|планир)\w*"
        r"|(?:tilda|тильд\w*)\s+исключ\w*"
        r"|(?:без|вместо|не\s+на|не\s+в|не\s+хотим|не\s+нужн\w*|"
        r"отказались\s+от|не\s+будем\s+использовать)\s+(?:tilda|тильд\w*)",
        text,
    ))
    tilda = tilda_mentioned and not tilda_ruled_out
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
    ignored = {"комп", "сайт", "ленд", "диза", "маке", "figm", "прое",
               "стра", "нуже", "корп", "мног", "мага", "рабо"}
    tokens = {word[:4] for word in re.findall(r"[а-яa-z]{5,}", text)} - ignored

    def rank(case: dict[str, str]) -> int:
        summary = f"{case['name']} {case['product']}".casefold()
        facts = f"{summary} {(index_terms or {}).get(case['id'], '')}".casefold()
        case_kind = kind(summary)
        # Generic Figma experience cannot prove mobile-app or e-commerce experience.
        if target_kind in {"interface", "store"} and case_kind != target_kind:
            return 0
        overlap = len(tokens & {word[:4] for word in re.findall(r"[а-яa-z]{5,}", facts)})
        same_kind = target_kind != "unknown" and case_kind == target_kind
        return (overlap * 10 + (20 if same_kind else 0)
                + (30 if tilda and case["platform"].startswith("Tilda") else 0))

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
    if "—" in text or "–" in text:
        issues.append("Замени длинное или среднее тире на обычный дефис с пробелами")
    if re.search(
        r"\bне\s+(?:просто|только)\b.{0,80}\b(?:а|но\s+и)\b",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        issues.append("Убери риторическое противопоставление «не просто/не только X, а Y»")
    if re.search(
        r"успешн\w*\s+(?:проект|опыт)|недавно.{0,25}(?:проект|заверш)|"
        r"(?:завершал|выполнял).{0,25}проект|"
        r"(?:есть|имею|имеется|обладаю).{0,12}опыт|"
        r"(?:приступить|начать|сделаю|завершу).{0,20}(?:сегодня|завтра)",
        text, re.IGNORECASE,
    ):
        issues.append("Убери неподтверждённые успехи, недавние работы и обещание начать сегодня/завтра")
    if re.search(
        r"готов\s+обсудить\s+(?:тз|техническ\w*\s+задан\w*)|"
        r"буду\s+рад\s+сотрудничеств\w*|современн\w*\s+и\s+продающ\w*\s+дизайн",
        text,
        re.IGNORECASE,
    ):
        issues.append("Убери шаблонную фразу и замени её конкретным действием по проекту")
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
