from pathlib import Path

import pytest

from freelance_bot.ai import GigaChatProjectAdvisor
from freelance_bot.config_texts import ConfigTextManager


def _manager(tmp_path: Path) -> ConfigTextManager:
    return ConfigTextManager(
        profile_path=tmp_path / "profile.txt",
        filter_prompt_path=tmp_path / "filter.txt",
        response_prompt_path=tmp_path / "response.txt",
    )


def test_replace_config_text_creates_backup(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    profile = manager.get("profile").path
    profile.write_text("Старый профиль\n", encoding="utf-8")

    applied = manager.replace("profile", "  Новый профиль  ")

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
    advisor._profile = "old"
    advisor._filter_prompt = "old"
    advisor._response_prompt = "old"

    advisor.update_config_text("response", "new response prompt")

    assert advisor._response_prompt == "new response prompt"
    assert advisor._profile == "old"
