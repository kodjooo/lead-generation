"""Загрузка конфигурации приложения из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class DatabaseSettings:
    """Параметры подключения к базе данных."""

    host: str
    port: int
    user: str
    password: str
    name: str

    def sync_dsn(self) -> str:
        """Формирует DSN для синхронного движка SQLAlchemy."""
        return (
            f"postgresql+psycopg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )


@dataclass(frozen=True)
class SMTPSettings:
    """Настройки SMTP отправителя."""

    host: str
    port: int
    username: str
    password: str
    sender: str
    sender_name: str | None


@dataclass(frozen=True)
class GoogleSheetsSettings:
    """Настройки доступа к Google Sheets."""

    sheet_id: str
    tab_name: str
    service_account_key_path: str | None
    service_account_key_json: str | None


@dataclass(frozen=True)
class SheetSyncSettings:
    """Параметры автоматической синхронизации Google Sheets."""

    enabled: bool
    interval_minutes: int
    batch_tag: str | None


@dataclass(frozen=True)
class Settings:
    """Глобальные настройки приложения."""

    timezone: str
    yandex_folder_id: str
    yandex_iam_token: str | None
    yandex_sa_key_path: str | None
    yandex_sa_key_json: str | None
    yandex_enforce_night_window: bool
    openai_api_key: str
    email_sending_enabled: bool
    redis_url: str
    database: DatabaseSettings
    smtp: SMTPSettings
    google_sheets: GoogleSheetsSettings
    sheet_sync: SheetSyncSettings


def _env(key: str, default: str = "") -> str:
    """Возвращает значение переменной окружения или значение по умолчанию."""
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Загружает настройки один раз и кэширует их для повторного использования."""
    db = DatabaseSettings(
        host=_env("POSTGRES_HOST", "db"),
        port=int(_env("POSTGRES_PORT", "5432")),
        user=_env("POSTGRES_USER", "leadgen"),
        password=_env("POSTGRES_PASSWORD", "leadgen_password"),
        name=_env("POSTGRES_DB", "leadgen"),
    )

    smtp = SMTPSettings(
        host=_env("SMTP_HOST", "smtp.gmail.com"),
        port=int(_env("SMTP_PORT", "587")),
        username=_env("SMTP_USERNAME", ""),
        password=_env("SMTP_PASSWORD", ""),
        sender=_env("SMTP_FROM_EMAIL", ""),
        sender_name=_env("SMTP_FROM_NAME") or None,
    )

    google_sheets = GoogleSheetsSettings(
        sheet_id=_env("GOOGLE_SHEET_ID"),
        tab_name=_env("GOOGLE_SHEET_TAB", "NICHES_INPUT"),
        service_account_key_path=_env("GOOGLE_SA_KEY_FILE") or None,
        service_account_key_json=_env("GOOGLE_SA_KEY_JSON") or None,
    )

    sheet_sync = SheetSyncSettings(
        enabled=_env("SHEET_SYNC_ENABLED", "false").lower() in {"1", "true", "yes"},
        interval_minutes=int(_env("SHEET_SYNC_INTERVAL_MINUTES", "60")),
        batch_tag=_env("SHEET_SYNC_BATCH_TAG") or None,
    )

    return Settings(
        timezone=_env("APP_TIMEZONE", "Europe/Moscow"),
        yandex_folder_id=_env("YANDEX_CLOUD_FOLDER_ID"),
        yandex_iam_token=_env("YANDEX_CLOUD_IAM_TOKEN") or None,
        yandex_sa_key_path=_env("YANDEX_CLOUD_SA_KEY_FILE") or None,
        yandex_sa_key_json=_env("YANDEX_CLOUD_SA_KEY_JSON") or None,
        yandex_enforce_night_window=_env_bool("YANDEX_ENFORCE_NIGHT_WINDOW", True),
        openai_api_key=_env("OPENAI_API_KEY"),
        email_sending_enabled=_env_bool("EMAIL_SENDING_ENABLED", True),
        redis_url=_env("REDIS_URL", "redis://redis:6379/0"),
        database=db,
        smtp=smtp,
        google_sheets=google_sheets,
        sheet_sync=sheet_sync,
    )
