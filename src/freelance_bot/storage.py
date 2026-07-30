import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from freelance_bot.models import AiAssessment, Project


class ProjectStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_projects (
                project_key TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_catalog (
                project_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price TEXT NOT NULL,
                url TEXT NOT NULL,
                category TEXT NOT NULL,
                published_at TEXT,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        catalog_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(project_catalog)").fetchall()
        }
        if "description" not in catalog_columns:
            self._connection.execute(
                "ALTER TABLE project_catalog ADD COLUMN description TEXT NOT NULL DEFAULT ''"
            )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_feedback (
                project_key TEXT PRIMARY KEY,
                decision TEXT NOT NULL CHECK(decision IN ('responded', 'rejected')),
                outcome TEXT CHECK(outcome IN ('client_replied', 'client_chose_other')),
                decision_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                outcome_at TEXT,
                FOREIGN KEY(project_key) REFERENCES project_catalog(project_key)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_ai_assessments (
                project_key TEXT PRIMARY KEY,
                suitable INTEGER NOT NULL CHECK(suitable IN (0, 1)),
                score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
                reason TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                response_text TEXT NOT NULL,
                filter_model TEXT NOT NULL,
                response_model TEXT NOT NULL,
                analyzed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_key) REFERENCES project_catalog(project_key)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_ai_responses (
                project_key TEXT PRIMARY KEY,
                response_text TEXT NOT NULL,
                response_model TEXT NOT NULL,
                generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_key) REFERENCES project_catalog(project_key)
            )
            """
        )
        assessment_columns = {
            str(row[1])
            for row in self._connection.execute(
                "PRAGMA table_info(project_ai_assessments)"
            ).fetchall()
        }
        if "summary" not in assessment_columns:
            self._connection.execute(
                "ALTER TABLE project_ai_assessments ADD COLUMN summary TEXT NOT NULL DEFAULT ''"
            )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS project_ai_rejections (
                project_key TEXT PRIMARY KEY,
                rejected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_key) REFERENCES project_catalog(project_key)
            )
            """
        )
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(seen_projects)").fetchall()
        }
        if "source" not in columns:
            self._connection.execute("ALTER TABLE seen_projects ADD COLUMN source TEXT")
        self._connection.execute(
            """
            UPDATE seen_projects
            SET source = substr(project_key, 1, instr(project_key, ':') - 1)
            WHERE source IS NULL AND instr(project_key, ':') > 0
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO state(name, value)
            VALUES ('statistics_started_at', CURRENT_TIMESTAMP)
            """
        )
        self._connection.commit()

    def is_seen(self, key: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM seen_projects WHERE project_key = ?", (key,)
        ).fetchone()
        return row is not None

    def mark_seen(
        self, key: str, source: str | None = None, *, count_for_statistics: bool = True
    ) -> None:
        resolved_source = (source or key.partition(":")[0]) if count_for_statistics else None
        self._connection.execute(
            """
            INSERT OR IGNORE INTO seen_projects(project_key, source, first_seen_at)
            VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (key, resolved_source),
        )
        self._connection.commit()

    def remember_project(self, project: Project) -> None:
        published_at = project.published_at.isoformat() if project.published_at else None
        self._connection.execute(
            """
            INSERT INTO project_catalog(
                project_key, source, external_id, title, description,
                price, url, category, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_key) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                price = excluded.price,
                url = excluded.url,
                category = excluded.category,
                published_at = excluded.published_at,
                last_seen_at = CURRENT_TIMESTAMP
            """,
            (
                project.key,
                project.source,
                project.external_id,
                project.title,
                project.description,
                project.price,
                project.url,
                project.category,
                published_at,
            ),
        )
        self._connection.commit()

    def remember_ai_response(self, project_key: str, response_text: str, model: str) -> None:
        self._connection.execute(
            """
            INSERT INTO project_ai_responses(
                project_key, response_text, response_model, generated_at
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(project_key) DO UPDATE SET
                response_text = excluded.response_text,
                response_model = excluded.response_model,
                generated_at = CURRENT_TIMESTAMP
            """,
            (project_key, response_text, model),
        )
        self._connection.commit()

    def get_ai_response(self, project_key: str) -> str:
        row = self._connection.execute(
            "SELECT response_text FROM project_ai_responses WHERE project_key = ?",
            (project_key,),
        ).fetchone()
        if row is not None:
            return str(row[0])
        legacy = self._connection.execute(
            "SELECT response_text FROM project_ai_assessments WHERE project_key = ?",
            (project_key,),
        ).fetchone()
        return str(legacy[0]) if legacy is not None and legacy[0] else ""

    def remember_ai_assessment(self, assessment: AiAssessment) -> None:
        self._connection.execute(
            """
            INSERT INTO project_ai_assessments(
                project_key, suitable, score, reason, summary, response_text,
                filter_model, response_model, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now'))
            ON CONFLICT(project_key) DO UPDATE SET
                suitable = excluded.suitable,
                score = excluded.score,
                reason = excluded.reason,
                summary = excluded.summary,
                response_text = excluded.response_text,
                filter_model = excluded.filter_model,
                response_model = excluded.response_model,
                analyzed_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
            """,
            (
                assessment.project_key,
                int(assessment.suitable),
                assessment.score,
                assessment.reason,
                assessment.summary,
                assessment.response_text,
                assessment.filter_model,
                assessment.response_model,
            ),
        )
        self._connection.commit()

    def get_ai_assessment(self, project_key: str) -> AiAssessment | None:
        row = self._connection.execute(
            """
            SELECT suitable, score, reason, summary, response_text, filter_model, response_model
            FROM project_ai_assessments
            WHERE project_key = ?
            """,
            (project_key,),
        ).fetchone()
        if row is None:
            return None
        return AiAssessment(
            project_key=project_key,
            suitable=bool(row[0]),
            score=int(row[1]),
            reason=str(row[2]),
            summary=str(row[3]),
            response_text=str(row[4]),
            filter_model=str(row[5]),
            response_model=str(row[6]),
        )

    def mark_ai_rejected(self, project_key: str) -> None:
        self._connection.execute(
            """
            INSERT OR IGNORE INTO project_ai_rejections(project_key, rejected_at)
            VALUES (?, strftime('%Y-%m-%d %H:%M:%f', 'now'))
            """,
            (project_key,),
        )
        self._connection.commit()

    def set_project_decision(self, project_key: str, decision: str) -> bool:
        if decision not in {"responded", "rejected"}:
            raise ValueError(f"Неизвестное решение: {decision}")
        if self.get_project(project_key) is None:
            return False
        row = self._connection.execute(
            "SELECT decision FROM project_feedback WHERE project_key = ?", (project_key,)
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO project_feedback(project_key, decision) VALUES (?, ?)",
                (project_key, decision),
            )
        elif row[0] != decision:
            self._connection.execute(
                """
                UPDATE project_feedback
                SET decision = ?, outcome = NULL, decision_at = CURRENT_TIMESTAMP, outcome_at = NULL
                WHERE project_key = ?
                """,
                (decision, project_key),
            )
        self._connection.commit()
        return True

    def toggle_project_decision(self, project_key: str, decision: str) -> str | None:
        if decision not in {"responded", "rejected"}:
            raise ValueError(f"Unknown project decision: {decision}")
        if self.get_project(project_key) is None:
            raise KeyError(project_key)
        current = self.get_project_feedback(project_key)
        if current is not None and current[0] == decision:
            self._connection.execute(
                "DELETE FROM project_feedback WHERE project_key = ?", (project_key,)
            )
            self._connection.commit()
            return None
        self._connection.execute(
            """
            INSERT INTO project_feedback(project_key, decision, outcome, decision_at, outcome_at)
            VALUES (?, ?, NULL, CURRENT_TIMESTAMP, NULL)
            ON CONFLICT(project_key) DO UPDATE SET
                decision = excluded.decision,
                outcome = NULL,
                decision_at = CURRENT_TIMESTAMP,
                outcome_at = NULL
            """,
            (project_key, decision),
        )
        self._connection.commit()
        return decision

    def set_project_outcome(self, project_key: str, outcome: str) -> bool:
        if outcome not in {"client_replied", "client_chose_other"}:
            raise ValueError(f"Неизвестный исход: {outcome}")
        cursor = self._connection.execute(
            """
            UPDATE project_feedback
            SET outcome = ?, outcome_at = CURRENT_TIMESTAMP
            WHERE project_key = ? AND decision = 'responded'
            """,
            (outcome, project_key),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def get_project(self, project_key: str) -> Project | None:
        row = self._connection.execute(
            """
            SELECT source, external_id, title, description, price, url, category, published_at
            FROM project_catalog WHERE project_key = ?
            """,
            (project_key,),
        ).fetchone()
        if row is None:
            return None
        published_at = datetime.fromisoformat(row[7]) if row[7] else None
        return Project(
            source=row[0],
            external_id=row[1],
            title=row[2],
            description=row[3],
            price=row[4],
            url=row[5],
            category=row[6],
            published_at=published_at,
        )

    def get_project_feedback(self, project_key: str) -> tuple[str, str | None] | None:
        row = self._connection.execute(
            "SELECT decision, outcome FROM project_feedback WHERE project_key = ?",
            (project_key,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]) if row[1] else None

    def feedback_counts(self) -> dict[str, int]:
        counts = {
            "responded": 0,
            "rejected": 0,
            "client_replied": 0,
            "client_chose_other": 0,
        }
        rows = self._connection.execute(
            """
            SELECT decision, outcome, COUNT(*)
            FROM project_feedback
            GROUP BY decision, outcome
            """
        ).fetchall()
        for decision, outcome, count in rows:
            counts[str(decision)] += int(count)
            if outcome:
                counts[str(outcome)] += int(count)
        return counts

    def active_responses(self, limit: int = 10) -> list[tuple[Project, str | None]]:
        rows = self._connection.execute(
            """
            SELECT c.project_key, f.outcome
            FROM project_feedback AS f
            JOIN project_catalog AS c USING(project_key)
            WHERE f.decision = 'responded'
              AND (f.outcome IS NULL OR f.outcome = 'client_replied')
            ORDER BY f.decision_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result: list[tuple[Project, str | None]] = []
        for project_key, outcome in rows:
            project = self.get_project(str(project_key))
            if project is not None:
                result.append((project, str(outcome) if outcome else None))
        return result

    def notifications_enabled(self, source: str) -> bool:
        return self.get_state(f"notifications:{source}") != "0"

    def toggle_notifications(self, source: str) -> bool:
        enabled = not self.notifications_enabled(source)
        self.set_state(f"notifications:{source}", "1" if enabled else "0")
        return enabled

    def project_statistics(self) -> dict[str, dict[str, int]]:
        periods = {"day": "-1 day", "week": "-7 days", "month": "-30 days"}
        result = {
            source: {period: 0 for period in periods} for source in ("Kwork", "FL.ru", "Profi.ru")
        }
        started_at = self.get_state("statistics_started_at") or "1970-01-01 00:00:00"
        for period, modifier in periods.items():
            rows = self._connection.execute(
                """
                SELECT catalog.source, COUNT(*)
                FROM project_ai_assessments AS assessment
                JOIN project_catalog AS catalog USING(project_key)
                WHERE assessment.suitable = 1
                  AND assessment.analyzed_at >= datetime('now', ?)
                  AND assessment.analyzed_at >= ?
                  AND catalog.source IN ('Kwork', 'FL.ru', 'Profi.ru')
                GROUP BY catalog.source
                """,
                (modifier, started_at),
            ).fetchall()
            for source, count in rows:
                result[str(source)][period] = int(count)
        return result

    def ai_rejected_statistics(self) -> dict[str, int]:
        periods = {"day": "-1 day", "week": "-7 days", "month": "-30 days"}
        result = {period: 0 for period in periods}
        started_at = self.get_state("statistics_started_at") or "1970-01-01 00:00:00"
        for period, modifier in periods.items():
            row = self._connection.execute(
                """
                SELECT COUNT(*)
                FROM project_ai_rejections
                WHERE rejected_at >= datetime('now', ?)
                  AND rejected_at >= ?
                """,
                (modifier, started_at),
            ).fetchone()
            result[period] = int(row[0]) if row is not None else 0
        return result

    def statistics_started_at(self) -> datetime:
        value = self.get_state("statistics_started_at")
        if value is None:
            return datetime.now(timezone.utc)
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)

    def reset_statistics(self) -> datetime:
        started_at = datetime.now(timezone.utc)
        self.set_state(
            "statistics_started_at",
            started_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
        )
        return started_at

    def get_state(self, name: str) -> str | None:
        row = self._connection.execute("SELECT value FROM state WHERE name = ?", (name,)).fetchone()
        return row[0] if row is not None else None

    def set_state(self, name: str, value: str) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO state(name, value) VALUES (?, ?)", (name, value)
        )
        self._connection.commit()

    def is_source_initialized(self, source: str) -> bool:
        row = self.get_state(f"initialized:{source}")
        return row == "1"

    def mark_source_initialized(self, source: str) -> None:
        self.set_state(f"initialized:{source}", "1")

    def is_initialized(self) -> bool:
        row = self.get_state("initialized")
        return row == "1"

    def mark_initialized(self) -> None:
        self.set_state("initialized", "1")

    def close(self) -> None:
        self._connection.close()
