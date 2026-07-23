from dataclasses import dataclass
import os
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

    @classmethod
    def from_env(cls) -> "Settings":
        interval = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))
        if interval < 60:
            raise ValueError("POLL_INTERVAL_SECONDS должен быть не меньше 60")
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
        )
