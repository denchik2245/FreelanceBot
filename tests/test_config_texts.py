from pathlib import Path

import pytest

from freelance_bot.ai import GigaChatProjectAdvisor
from freelance_bot.config_texts import ConfigTextManager


def _manager(tmp_path: Path) -> ConfigTextManager:
    return ConfigTextManager(
        filter_prompt_path=tmp_path / "filter.txt",
    )


def test_replace_config_text_creates_backup(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    profile = manager.get("filter").path
    profile.write_text("Старый профиль\n", encoding="utf-8")

    applied = manager.replace("filter", "  Новый профиль  ")

    assert applied == "Новый профиль"
    assert profile.read_text(encoding="utf-8") == "Новый профиль\n"
    assert profile.with_suffix(".txt.bak").read_text(encoding="utf-8") == "Старый профиль\n"


def test_config_text_manager_rejects_empty_and_unknown_text(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="не может быть пустым"):
        manager.replace("filter", "  \n")
    with pytest.raises(ValueError, match="Неизвестный"):
        manager.replace("../secret", "text")


def test_advisor_applies_config_text_without_restart() -> None:
    advisor = object.__new__(GigaChatProjectAdvisor)
    advisor._filter_prompt = "old"

    advisor.update_config_text("filter", "new filter prompt")

    assert advisor._filter_prompt == "new filter prompt"
