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


@dataclass(frozen=True)
class Settings:
    """Глобальные настройки приложения."""

    timezone: str
    yandex_iam_token: str
    yandex_folder_id: str
    openai_api_key: str
    redis_url: str
    database: DatabaseSettings
    smtp: SMTPSettings


def _env(key: str, default: str = "") -> str:
    """Возвращает значение переменной окружения или значение по умолчанию."""
    return os.getenv(key, default).strip()


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
    )

    return Settings(
        timezone=_env("APP_TIMEZONE", "Europe/Moscow"),
        yandex_iam_token=_env("YANDEX_CLOUD_IAM_TOKEN"),
        yandex_folder_id=_env("YANDEX_CLOUD_FOLDER_ID"),
        openai_api_key=_env("OPENAI_API_KEY"),
        redis_url=_env("REDIS_URL", "redis://redis:6379/0"),
        database=db,
        smtp=smtp,
    )

