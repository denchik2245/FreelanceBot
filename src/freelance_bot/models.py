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
    publication_id: str = ""

    @property
    def canonical_key(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def key(self) -> str:
        if self.publication_id:
            return f"{self.canonical_key}@{self.publication_id}"
        return self.canonical_key


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
    filter_revision: str = ""
