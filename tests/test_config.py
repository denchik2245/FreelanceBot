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
    assert settings.ai_min_score == 82
    assert settings.ai_filter_prompt_path.as_posix() == "config/prompts/project_filter.txt"
    assert settings.ai_response_prompt_path.as_posix() == "config/prompts/response_writer.txt"
    assert settings.gigachat_filter_model == "GigaChat-2-Pro"
    assert settings.gigachat_filter_fallback_model == "GigaChat-2-Max"
    assert settings.gigachat_response_model == "GigaChat-3-Ultra"


def test_ai_rejects_empty_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "secret")
    monkeypatch.setenv("GIGACHAT_FILTER_MODEL", "  ")

    with pytest.raises(ValueError, match="не могут быть пустыми"):
        Settings.from_env()


def test_deepseek_needs_its_own_key_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
    monkeypatch.setenv("GIGACHAT_FILTER_MODEL", "")
    settings = Settings.from_env()
    assert settings.ai_provider == "deepseek"
    assert settings.deepseek_filter_model == "deepseek-flash"
    assert settings.deepseek_filter_fallback_model == "deepseek-flash"
    assert settings.deepseek_response_model == "deepseek-flash"


def test_deepseek_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "giga-key")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        Settings.from_env()


@pytest.mark.parametrize("field,value,match", [
    ("AI_PROVIDER", "other", "AI_PROVIDER"),
    ("DEEPSEEK_BASE_URL", "http://api.deepseek.com", "HTTPS"),
    ("DEEPSEEK_FILTER_MODEL", "", "не могут быть пустыми"),
    ("DEEPSEEK_FILTER_FALLBACK_MODEL", "", "не могут быть пустыми"),
    ("DEEPSEEK_RESPONSE_MODEL", "", "не могут быть пустыми"),
])
def test_deepseek_invalid_settings(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str, match: str,
) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("AI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError, match=match):
        Settings.from_env()


def test_profi_credentials_must_be_configured_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("PROFI_LOGIN", "specialist")
    monkeypatch.delenv("PROFI_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="должны быть заданы вместе"):
        Settings.from_env()


def test_gmail_settings_are_loaded_and_app_password_spaces_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("FL_GMAIL_ADDRESS", "freelancer@gmail.com")
    monkeypatch.setenv("FL_GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("FL_MAIL_POLL_INTERVAL_SECONDS", "45")

    settings = Settings.from_env()

    assert settings.fl_gmail_address == "freelancer@gmail.com"
    assert settings.fl_gmail_app_password == "abcdefghijklmnop"
    assert settings.fl_mail_poll_interval_seconds == 45


def test_gmail_credentials_must_be_configured_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_environment(monkeypatch)
    monkeypatch.setenv("FL_GMAIL_ADDRESS", "freelancer@gmail.com")
    monkeypatch.delenv("FL_GMAIL_APP_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="FL_GMAIL_ADDRESS"):
        Settings.from_env()
