from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Project:
    source: str
    external_id: str
    title: str
    description: str
    price: str
    url: str
    category: str
    published_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"


@dataclass(frozen=True, slots=True)
class AiAssessment:
    project_key: str
    suitable: bool
    score: int
    reason: str
    response_text: str
    filter_model: str
    response_model: str
    summary: str = ""
