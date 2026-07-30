import os
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Не задана обязательная переменная {name}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    vk_group_token: str
    vk_user_id: int
    vk_api_version: str
    kwork_login: str
    kwork_password: str
    profi_login: str
    profi_password: str
    poll_interval_seconds: int
    database_path: Path
    send_existing_on_first_run: bool
    log_level: str
    ai_enabled: bool
    ai_min_score: int
    ai_profile_path: Path
    ai_filter_prompt_path: Path
    ai_response_prompt_path: Path
    gigachat_credentials: str
    gigachat_scope: str
    gigachat_base_url: str
    gigachat_ca_bundle_file: Path | None
    gigachat_filter_model: str
    gigachat_response_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        interval = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))
        if interval < 60:
            raise ValueError("POLL_INTERVAL_SECONDS должен быть не меньше 60")
        ai_enabled = _bool("AI_ENABLED")
        ai_min_score = int(os.getenv("AI_MIN_SCORE", "70"))
        if not 0 <= ai_min_score <= 100:
            raise ValueError("AI_MIN_SCORE должен быть от 0 до 100")
        gigachat_credentials = os.getenv("GIGACHAT_CREDENTIALS", "").strip()
        if ai_enabled and not gigachat_credentials:
            raise ValueError("AI_ENABLED=true, но не задан обязательный GIGACHAT_CREDENTIALS")
        gigachat_filter_model = os.getenv("GIGACHAT_FILTER_MODEL", "GigaChat-2").strip()
        gigachat_response_model = os.getenv(
            "GIGACHAT_RESPONSE_MODEL", "GigaChat-2-Pro"
        ).strip()
        if ai_enabled and (not gigachat_filter_model or not gigachat_response_model):
            raise ValueError("AI-модели GigaChat не могут быть пустыми")
        ca_bundle = os.getenv("GIGACHAT_CA_BUNDLE_FILE", "").strip()
        return cls(
            vk_group_token=_required("VK_GROUP_TOKEN"),
            vk_user_id=int(_required("VK_USER_ID")),
            vk_api_version=os.getenv("VK_API_VERSION", "5.199"),
            kwork_login=_required("KWORK_LOGIN"),
            kwork_password=_required("KWORK_PASSWORD"),
            profi_login=os.getenv("PROFI_LOGIN", "").strip(),
            profi_password=os.getenv("PROFI_PASSWORD", "").strip(),
            poll_interval_seconds=interval,
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
            send_existing_on_first_run=_bool("SEND_EXISTING_ON_FIRST_RUN"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            ai_enabled=ai_enabled,
            ai_min_score=ai_min_score,
            ai_profile_path=Path(os.getenv("AI_PROFILE_PATH", "config/freelancer_profile.txt")),
            ai_filter_prompt_path=Path(
                os.getenv("AI_FILTER_PROMPT_PATH", "config/prompts/project_filter.txt")
            ),
            ai_response_prompt_path=Path(
                os.getenv("AI_RESPONSE_PROMPT_PATH", "config/prompts/response_writer.txt")
            ),
            gigachat_credentials=gigachat_credentials,
            gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip(),
            gigachat_base_url=os.getenv("GIGACHAT_BASE_URL", "https://api.giga.chat/v1").strip(),
            gigachat_ca_bundle_file=Path(ca_bundle) if ca_bundle else None,
            gigachat_filter_model=gigachat_filter_model,
            gigachat_response_model=gigachat_response_model,
        )
