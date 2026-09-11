from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from freelance_bot.portfolio import parse_portfolio

MAX_CONFIG_TEXT_BYTES = 100_000


@dataclass(frozen=True, slots=True)
class EditableConfigText:
    key: str
    label: str
    path: Path


class ConfigTextManager:
    """Read and atomically replace AI prompts, profile and validated portfolio exposed in VK."""

    def __init__(
        self,
        *,
        profile_path: Path,
        filter_prompt_path: Path,
        response_prompt_path: Path,
        portfolio_path: Path | None = None,
    ) -> None:
        self._items = {
            "profile": EditableConfigText("profile", "Профиль исполнителя", profile_path),
            "filter": EditableConfigText("filter", "Промпт отбора", filter_prompt_path),
            "response": EditableConfigText("response", "Промпт отклика", response_prompt_path),
        }
        if portfolio_path is not None:
            self._items["portfolio"] = EditableConfigText(
                "portfolio", "Портфолио (JSON)", portfolio_path
            )

    def items(self) -> tuple[EditableConfigText, ...]:
        return tuple(self._items.values())

    def get(self, key: str) -> EditableConfigText:
        try:
            return self._items[key]
        except KeyError as error:
            raise ValueError("Неизвестный AI-текст") from error

    def read(self, key: str) -> str:
        item = self.get(key)
        try:
            return item.path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuntimeError(f"Не удалось прочитать {item.path}: {error}") from error

    def replace(self, key: str, text: str) -> str:
        item = self.get(key)
        normalized = text.strip()
        if not normalized:
            raise ValueError("AI-текст не может быть пустым")
        if key == "portfolio":
            parse_portfolio(normalized)
        encoded = normalized.encode("utf-8")
        if len(encoded) > MAX_CONFIG_TEXT_BYTES:
            raise ValueError("AI-текст больше 100 КБ")

        path = item.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(normalized + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return normalized
