import pytest

from freelance_bot.config import Settings


def _base_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VK_GROUP_TOKEN", "vk-token")
    monkeypatch.setenv("VK_USER_ID", "123")
    monkeypatch.setenv("KWORK_LOGIN", "login")
    monkeypatch.setenv("KWORK_PASSWORD", "password")


def test_ai_requires_gigachat_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)

    with pytest.raises(ValueError, match="GIGACHAT_CREDENTIALS"):
        Settings.from_env()


def test_ai_settings_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "secret")
    monkeypatch.setenv("AI_MIN_SCORE", "82")

    settings = Settings.from_env()

    assert settings.ai_enabled
    assert not settings.ai_fail_open
    assert settings.ai_min_score == 82
    assert settings.ai_filter_prompt_path.as_posix() == "config/prompts/project_filter.txt"
    assert settings.ai_response_prompt_path.as_posix() == "config/prompts/response_writer.txt"
    assert settings.gigachat_filter_model == "GigaChat-2-Pro"
    assert settings.gigachat_response_model == "GigaChat-2-Pro"
