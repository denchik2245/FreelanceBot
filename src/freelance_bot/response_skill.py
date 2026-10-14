"""Load the project-local writing skill; models cannot read local files themselves."""

from pathlib import Path


def load_response_skill(path: Path) -> str:
    parts = []
    for relative in ("SKILL.md", "references/writing-style.md"):
        source = path / relative
        try:
            text = source.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError(f"Не удалось прочитать инструкции скилла {source}") from error
        if not text:
            raise ValueError(f"Инструкции скилла пусты: {source}")
        parts.append(f"ФАЙЛ {relative}:\n{text}")
    return "\n\n".join(parts)
