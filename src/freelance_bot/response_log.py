"""Per-project response tracker.

The bot records a snapshot when «Откликнулся» is pressed. Outcomes (read, replied,
ordered…) are filled once a week from the exchanges' own «my responses» pages through
the ``freelance_bot.tracker`` command, so the user presses nothing else.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from freelance_bot.models import AiAssessment, Project

SOURCES = ("Kwork", "FL.ru", "Profi.ru")
OUTCOMES = ("ordered", "chose_other", "refused", "closed")
OUTCOME_LABELS = {
    "ordered": "заказ",
    "chose_other": "выбрали другого",
    "refused": "отказ",
    "closed": "закрыт без выбора",
}
ORIGINS = ("button", "review", "legacy")
FAST_RESPONSE_MINUTES = 60
STALE_DIALOG_DAYS = 3
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_INT_FIELDS = ("offers_at_response", "my_price", "my_days", "order_amount")
_BOOL_FIELDS = ("concept", "viewed", "replied", "shortlisted")
_TEXT_FIELDS = ("title", "note", "level", "level_reason")
EDITABLE_FIELDS = (*_INT_FIELDS, *_BOOL_FIELDS, *_TEXT_FIELDS, "outcome", "responded_at")

_URL_ID = {
    "Kwork": re.compile(r"kwork\.ru/projects/(\d+)"),
    "FL.ru": re.compile(r"fl\.ru/projects/(\d+)"),
    "Profi.ru": re.compile(r"profi\.ru/.*?(\d{5,})"),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("T", " ")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def canonical_key(project_key: str) -> str:
    return project_key.split("@", 1)[0]


def key_from_url(url: str) -> str | None:
    for source, pattern in _URL_ID.items():
        match = pattern.search(url)
        if match:
            return f"{source}:{match.group(1)}"
    return None


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS response_log (
            project_key TEXT PRIMARY KEY,
            canonical_key TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            budget TEXT NOT NULL DEFAULT '',
            published_at TEXT,
            responded_at TEXT NOT NULL,
            origin TEXT NOT NULL,
            level TEXT,
            level_reason TEXT,
            offers_at_notice INTEGER,
            offers_at_response INTEGER,
            client_projects_count INTEGER,
            client_hired_percent REAL,
            my_price INTEGER,
            my_days INTEGER,
            concept INTEGER,
            viewed INTEGER,
            replied INTEGER,
            shortlisted INTEGER,
            outcome TEXT,
            order_amount INTEGER,
            note TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT,
            replied_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS response_log_canonical ON response_log(canonical_key)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS response_log_responded ON response_log(responded_at)"
    )


def _rating(
    project: Project, assessment: AiAssessment | None, at: datetime
) -> tuple[str, str]:
    # Imported lazily: project_rating → project_filters → storage → this module.
    from freelance_bot.project_rating import rate_project

    # Rate as of the response: the "older than two weeks" rule must not fire later.
    rating = rate_project(project, assessment, now=at)
    return rating.level, rating.reason


def relevel_legacy(
    connection: sqlite3.Connection, load_project: Any, load_assessment: Any
) -> int:
    """Recompute levels of seeded entries that were once rated against the seeding date."""
    rows = connection.execute(
        "SELECT project_key, responded_at FROM response_log WHERE origin = 'legacy'"
    ).fetchall()
    changed = 0
    for project_key, responded_at in rows:
        project = load_project(project_key)
        if project is None:
            continue
        level, reason = _rating(
            project, load_assessment(project_key), parse_timestamp(responded_at) or _now()
        )
        connection.execute(
            "UPDATE response_log SET level = ?, level_reason = ? WHERE project_key = ?",
            (level, reason, project_key),
        )
        changed += 1
    return changed


def seed_from_feedback(
    connection: sqlite3.Connection,
    load_project: Any,
    load_assessment: Any,
) -> int:
    """Copy older «Откликнулся» presses once, so the history is not lost."""
    rows = connection.execute(
        """
        SELECT f.project_key, f.decision_at
        FROM project_feedback AS f
        LEFT JOIN response_log AS r ON r.project_key = f.project_key
        WHERE f.decision = 'responded' AND r.project_key IS NULL
        """
    ).fetchall()
    added = 0
    for project_key, decision_at in rows:
        project = load_project(project_key)
        if project is None:
            continue
        responded_at = parse_timestamp(decision_at) or _now()
        _insert_snapshot(
            connection, project, load_assessment(project_key), responded_at, origin="legacy"
        )
        added += 1
    return added


def _insert_snapshot(
    connection: sqlite3.Connection,
    project: Project,
    assessment: AiAssessment | None,
    responded_at: datetime,
    *,
    origin: str,
) -> None:
    level, reason = _rating(project, assessment, responded_at)
    connection.execute(
        """
        INSERT INTO response_log(
            project_key, canonical_key, source, title, url, budget, published_at,
            responded_at, origin, level, level_reason, offers_at_notice,
            client_projects_count, client_hired_percent, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_key) DO NOTHING
        """,
        (
            project.key,
            project.canonical_key,
            project.source,
            project.title,
            project.url,
            project.price,
            _ts(project.published_at) if project.published_at else None,
            _ts(responded_at),
            origin,
            level,
            reason,
            project.offers_count,
            project.client_projects_count,
            project.client_hired_percent,
            _ts(_now()),
        ),
    )


@dataclass(slots=True)
class Entry:
    project_key: str
    source: str
    title: str
    url: str
    budget: str
    published_at: datetime | None
    responded_at: datetime
    origin: str
    level: str | None
    level_reason: str | None
    offers_at_notice: int | None
    offers_at_response: int | None
    client_hired_percent: float | None
    my_price: int | None
    my_days: int | None
    concept: bool | None
    viewed: bool | None
    replied: bool | None
    shortlisted: bool | None
    outcome: str | None
    order_amount: int | None
    note: str
    reviewed_at: datetime | None
    replied_at: datetime | None

    @property
    def offers(self) -> int | None:
        return self.offers_at_response if self.offers_at_response is not None else (
            self.offers_at_notice
        )

    @property
    def minutes_after_publication(self) -> int | None:
        if self.published_at is None or self.origin == "review":
            return None
        delta = self.responded_at - self.published_at
        return max(0, int(delta.total_seconds() // 60))

    def as_dict(self) -> dict[str, Any]:
        def fmt(value: datetime | None) -> str | None:
            return _ts(value) if value else None

        return {
            "project_key": self.project_key,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "budget": self.budget,
            "published_at": fmt(self.published_at),
            "responded_at": fmt(self.responded_at),
            "minutes_after_publication": self.minutes_after_publication,
            "origin": self.origin,
            "level": self.level,
            "level_reason": self.level_reason,
            "offers_at_notice": self.offers_at_notice,
            "offers_at_response": self.offers_at_response,
            "client_hired_percent": self.client_hired_percent,
            "my_price": self.my_price,
            "my_days": self.my_days,
            "concept": self.concept,
            "viewed": self.viewed,
            "replied": self.replied,
            "shortlisted": self.shortlisted,
            "outcome": self.outcome,
            "order_amount": self.order_amount,
            "note": self.note,
            "reviewed_at": fmt(self.reviewed_at),
        }


_COLUMNS = (
    "project_key, source, title, url, budget, published_at, responded_at, origin, level, "
    "level_reason, offers_at_notice, offers_at_response, client_hired_percent, my_price, "
    "my_days, concept, viewed, replied, shortlisted, outcome, order_amount, note, "
    "reviewed_at, replied_at"
)


def _bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _entry(row: tuple) -> Entry:
    return Entry(
        project_key=row[0],
        source=row[1],
        title=row[2],
        url=row[3],
        budget=row[4],
        published_at=parse_timestamp(row[5]),
        responded_at=parse_timestamp(row[6]) or _now(),
        origin=row[7],
        level=row[8],
        level_reason=row[9],
        offers_at_notice=row[10],
        offers_at_response=row[11],
        client_hired_percent=row[12],
        my_price=row[13],
        my_days=row[14],
        concept=_bool(row[15]),
        viewed=_bool(row[16]),
        replied=_bool(row[17]),
        shortlisted=_bool(row[18]),
        outcome=row[19],
        order_amount=row[20],
        note=row[21] or "",
        reviewed_at=parse_timestamp(row[22]),
        replied_at=parse_timestamp(row[23]),
    )


class ResponseLog:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # --- bot side -----------------------------------------------------------------

    def record(
        self,
        project: Project,
        assessment: AiAssessment | None,
        *,
        now: datetime | None = None,
    ) -> None:
        with self._connection:
            _insert_snapshot(
                self._connection, project, assessment, now or _now(), origin="button"
            )

    def discard(self, project_key: str) -> bool:
        """Undo a mistaken press unless the weekly review already filled the entry."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM response_log WHERE project_key = ? AND reviewed_at IS NULL",
                (project_key,),
            )
        return cursor.rowcount > 0

    # --- queries ------------------------------------------------------------------

    def get(self, project_key: str) -> Entry | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM response_log WHERE project_key = ?", (project_key,)
        ).fetchone()
        return _entry(row) if row else None

    def entries(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        include_legacy: bool = False,
    ) -> list[Entry]:
        clauses, params = [], []
        if since is not None:
            clauses.append("responded_at >= ?")
            params.append(_ts(since))
        if until is not None:
            clauses.append("responded_at <= ?")
            params.append(_ts(until))
        if not include_legacy:
            # Old presses count once the weekly review has checked them on the exchange.
            clauses.append("(origin != 'legacy' OR reviewed_at IS NOT NULL)")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM response_log {where} ORDER BY responded_at", params
        ).fetchall()
        return [_entry(row) for row in rows]

    def open_entries(self, *, max_age_days: int = 45, now: datetime | None = None) -> list[Entry]:
        """Responses without a final outcome that are still worth checking."""
        since = (now or _now()) - timedelta(days=max_age_days)
        return [
            entry
            for entry in self.entries(since=since, include_legacy=True)
            if entry.outcome is None
        ]

    # --- weekly review ------------------------------------------------------------

    def _find_key(self, item: dict[str, Any]) -> str | None:
        candidates = []
        if item.get("project_key"):
            candidates.append(str(item["project_key"]))
        if item.get("source") and item.get("external_id"):
            candidates.append(f"{item['source']}:{item['external_id']}")
        if item.get("url"):
            parsed = key_from_url(str(item["url"]))
            if parsed:
                candidates.append(parsed)
        for candidate in candidates:
            row = self._connection.execute(
                """
                SELECT project_key FROM response_log
                WHERE project_key = ? OR canonical_key = ?
                ORDER BY responded_at DESC LIMIT 1
                """,
                (candidate, canonical_key(candidate)),
            ).fetchone()
            if row:
                return str(row[0])
        return candidates[0] if candidates else None

    def apply(
        self,
        items: list[dict[str, Any]],
        *,
        load_project: Any = None,
        load_assessment: Any = None,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Upsert review results. Unknown responses are created with origin=review."""
        moment = now or _now()
        result = {"updated": 0, "created": 0}
        with self._connection:
            for raw in items:
                if not isinstance(raw, dict):
                    raise ValueError("Каждая запись должна быть объектом JSON")
                item = dict(raw)
                key = self._find_key(item)
                if key is None:
                    raise ValueError(f"Не удалось определить проект: {raw}")
                changes = _validated_changes(item)
                exists = self._connection.execute(
                    "SELECT 1 FROM response_log WHERE project_key = ?", (key,)
                ).fetchone()
                if not exists:
                    self._create_from_review(key, item, changes, load_project, load_assessment)
                    result["created"] += 1
                else:
                    result["updated"] += 1
                if changes.get("replied") and "replied_at" not in changes:
                    current = self._connection.execute(
                        "SELECT replied_at FROM response_log WHERE project_key = ?", (key,)
                    ).fetchone()
                    if current and current[0] is None:
                        changes["replied_at"] = _ts(moment)
                changes["reviewed_at"] = _ts(moment)
                changes["updated_at"] = _ts(moment)
                assignments = ", ".join(f"{column} = ?" for column in changes)
                self._connection.execute(
                    f"UPDATE response_log SET {assignments} WHERE project_key = ?",
                    (*changes.values(), key),
                )
        return result

    def _create_from_review(
        self,
        key: str,
        item: dict[str, Any],
        changes: dict[str, Any],
        load_project: Any,
        load_assessment: Any,
    ) -> None:
        source = str(item.get("source") or key.partition(":")[0])
        if source not in SOURCES:
            raise ValueError(f"Неизвестная биржа: {source}")
        project = load_project(key) if load_project else None
        responded_at = parse_timestamp(changes.get("responded_at")) or _now()
        if project is not None:
            assessment = load_assessment(key) if load_assessment else None
            _insert_snapshot(self._connection, project, assessment, responded_at, origin="review")
            return
        self._connection.execute(
            """
            INSERT INTO response_log(
                project_key, canonical_key, source, title, url, budget, published_at,
                responded_at, origin, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'review', ?)
            """,
            (
                key,
                canonical_key(key),
                source,
                str(item.get("title") or ""),
                str(item.get("url") or ""),
                str(item.get("budget") or ""),
                _ts(parse_timestamp(item["published_at"])) if item.get("published_at") else None,
                _ts(responded_at),
                _ts(_now()),
            ),
        )

    def mark_reviewed(self, at: datetime | None = None) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO state(name, value) VALUES ('tracker:last_review', ?)",
                (_ts(at or _now()),),
            )

    def last_review(self) -> datetime | None:
        row = self._connection.execute(
            "SELECT value FROM state WHERE name = 'tracker:last_review'"
        ).fetchone()
        return parse_timestamp(row[0]) if row else None


def _validated_changes(item: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for name in EDITABLE_FIELDS:
        if name not in item:
            continue
        value = item[name]
        if name in _INT_FIELDS:
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int | float):
                    value = int(re.sub(r"[^\d]", "", str(value)) or "-1")
                value = int(value)
                if value < 0:
                    raise ValueError(f"{name}: нужно неотрицательное число")
        elif name in _BOOL_FIELDS:
            if value is not None:
                if not isinstance(value, bool):
                    raise ValueError(f"{name}: нужно true, false или null")
                value = int(value)
        elif name == "outcome":
            if value is not None and value not in OUTCOMES:
                raise ValueError(f"outcome: одно из {', '.join(OUTCOMES)} или null")
        elif name == "responded_at":
            parsed = parse_timestamp(value)
            if parsed is None:
                continue
            value = _ts(parsed)
        elif name == "level":
            if value is not None and value not in {"А", "Б", "В"}:
                raise ValueError("level: А, Б или В (кириллицей)")
        else:
            value = "" if value is None and name == "note" else value
            value = None if value is None else str(value)
        changes[name] = value
    if changes.get("outcome") == "ordered":
        changes.setdefault("viewed", 1)
        changes.setdefault("replied", 1)
    if changes.get("replied") or changes.get("shortlisted"):
        changes.setdefault("viewed", 1)
    return changes


# --- summaries ------------------------------------------------------------------


@dataclass(slots=True)
class Bucket:
    total: int = 0
    viewed_known: int = 0
    viewed: int = 0
    replied: int = 0
    ordered: int = 0
    revenue: int = 0
    keys: list[str] = field(default_factory=list)

    def add(self, entry: Entry) -> None:
        self.total += 1
        if entry.viewed is not None:
            self.viewed_known += 1
            self.viewed += int(entry.viewed)
        self.replied += int(bool(entry.replied))
        if entry.outcome == "ordered":
            self.ordered += 1
            self.revenue += entry.order_amount or 0


def _percent(part: int, whole: int) -> str:
    return f"{round(part / whole * 100)}%" if whole else "—"


def _bucket_line(label: str, bucket: Bucket) -> str:
    read = (
        f"прочитано {bucket.viewed} из {bucket.viewed_known}"
        if bucket.viewed_known
        else "прочтения не проверены"
    )
    line = f"{label}: {bucket.total} → {read}, написали {bucket.replied}, заказов {bucket.ordered}"
    return line


def _crowd(entry: Entry) -> str:
    offers = entry.offers
    if offers is None:
        return "неизвестно"
    if offers < 40:
        return "до 40"
    if offers <= 120:
        return "40–120"
    return "больше 120"


def summarize(entries: list[Entry]) -> dict[str, Any]:
    total = Bucket()
    by_source: dict[str, Bucket] = {}
    by_level: dict[str, Bucket] = {}
    by_concept: dict[str, Bucket] = {}
    by_speed: dict[str, Bucket] = {}
    by_crowd: dict[str, Bucket] = {}
    for entry in entries:
        total.add(entry)
        by_source.setdefault(entry.source, Bucket()).add(entry)
        by_level.setdefault(entry.level or "?", Bucket()).add(entry)
        if entry.concept is not None:
            by_concept.setdefault("с концепцией" if entry.concept else "без концепции",
                                  Bucket()).add(entry)
        minutes = entry.minutes_after_publication
        if minutes is not None:
            speed = "до 1 часа" if minutes <= FAST_RESPONSE_MINUTES else "позже часа"
            by_speed.setdefault(speed, Bucket()).add(entry)
        by_crowd.setdefault(_crowd(entry), Bucket()).add(entry)
    return {
        "total": total,
        "source": by_source,
        "level": by_level,
        "concept": by_concept,
        "speed": by_speed,
        "crowd": by_crowd,
    }


def stale_dialogs(
    entries: list[Entry], *, now: datetime | None = None, days: int = STALE_DIALOG_DAYS
) -> list[tuple[Entry, int]]:
    moment = now or _now()
    result = []
    for entry in entries:
        if entry.replied and entry.outcome is None:
            since = entry.replied_at or entry.reviewed_at or entry.responded_at
            age = (moment - since).days
            if age >= days:
                result.append((entry, age))
    return sorted(result, key=lambda item: -item[1])


def _clip(text: str, limit: int = 60) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def format_digest(
    log: ResponseLog,
    *,
    now: datetime | None = None,
    display_tz: Any = UTC,
    days: int = 7,
) -> str:
    moment = now or _now()
    since = moment - timedelta(days=days)
    week = log.entries(since=since, until=moment)
    month = log.entries(since=moment - timedelta(days=28), until=moment)
    local_since = since.astimezone(display_tz)
    local_now = moment.astimezone(display_tz)
    lines = [f"📈 Отклики {local_since:%d.%m}–{local_now:%d.%m}"]
    if not week:
        lines.append("За неделю откликов не отмечено.")
    else:
        stats = summarize(week)
        total: Bucket = stats["total"]
        sources = ", ".join(
            f"{name} {stats['source'][name].total}" for name in SOURCES if name in stats["source"]
        )
        lines.append(f"Откликов {total.total} ({sources})")
        lines.append(
            "Прочитано "
            + (f"{total.viewed} из {total.viewed_known} ({_percent(total.viewed, total.viewed_known)})"
               if total.viewed_known else "— ещё не проверено на биржах")
        )
        lines.append(f"Написали {total.replied} · заказов {total.ordered}"
                     + (f" на {total.revenue:,} ₽".replace(",", " ") if total.revenue else ""))
        lines.append("")
        lines.append("По уровням")
        for level in ("А", "Б", "В", "?"):
            if level in stats["level"]:
                lines.append(_bucket_line(level, stats["level"][level]))
        if stats["concept"]:
            lines.append("")
            lines.append("Концепция первого экрана")
            for label, bucket in stats["concept"].items():
                lines.append(_bucket_line(label, bucket))
        if stats["speed"]:
            lines.append("")
            lines.append("Скорость отклика")
            for label in ("до 1 часа", "позже часа"):
                if label in stats["speed"]:
                    lines.append(_bucket_line(label, stats["speed"][label]))
        lines.append("")
        lines.append("Откликов у проекта")
        for label in ("до 40", "40–120", "больше 120", "неизвестно"):
            if label in stats["crowd"]:
                lines.append(_bucket_line(label, stats["crowd"][label]))
        unchecked = sum(1 for entry in week if entry.reviewed_at is None)
        if unchecked:
            lines.append("")
            lines.append(f"Не сверено с биржами: {unchecked}. Попросите Claude сделать разбор.")
    month_stats = summarize(month)["total"] if month else Bucket()
    lines.append("")
    lines.append(
        f"За 4 недели: откликов {month_stats.total}, "
        f"прочитано {_percent(month_stats.viewed, month_stats.viewed_known)}, "
        f"написали {month_stats.replied}, заказов {month_stats.ordered}"
    )
    hanging = stale_dialogs(log.open_entries(now=moment), now=moment)
    if hanging:
        lines.append("")
        lines.append("🔔 Напомнить о себе")
        for entry, age in hanging[:8]:
            lines.append(f"• {entry.source}: {_clip(entry.title)} — {age} дн. без итога")
    return "\n".join(lines)


def format_panel(log: ResponseLog, *, now: datetime | None = None) -> str:
    moment = now or _now()
    lines = ["📨 Отклики"]
    for days, label in ((7, "За 7 дней"), (30, "За 30 дней")):
        entries = log.entries(since=moment - timedelta(days=days), until=moment)
        stats = summarize(entries)
        total: Bucket = stats["total"]
        lines.extend(("", label, f"Откликнулся — {total.total}"))
        for source in SOURCES:
            if source in stats["source"]:
                name = "Kwork.ru" if source == "Kwork" else source
                lines.append(f"  {name} — {stats['source'][source].total}")
        lines.append(
            f"Прочитано — {total.viewed} из {total.viewed_known}"
            if total.viewed_known
            else "Прочитано — ещё не сверено"
        )
        lines.append(f"Написали — {total.replied}")
        lines.append(f"Заказали — {total.ordered}")
        levels = ", ".join(
            f"{level} {stats['level'][level].total}"
            for level in ("А", "Б", "В")
            if level in stats["level"]
        )
        if levels:
            lines.append(f"Уровни — {levels}")
    last = log.last_review()
    lines.append("")
    lines.append(
        f"Последний разбор с биржами: {last:%d.%m.%Y}" if last else "Разбора с биржами ещё не было"
    )
    lines.append(
        "Отмечайте только «Откликнулся» на карточке. Прочтения, ответы и заказы "
        "Claude сверяет с Kwork и FL раз в неделю."
    )
    return "\n".join(lines)
