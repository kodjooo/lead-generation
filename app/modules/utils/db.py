"""Инструменты для работы с базой данных и миграциями."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import DatabaseSettings, get_settings

LOGGER = logging.getLogger("app.db")
DEFAULT_MIGRATIONS_PATH = Path(__file__).resolve().parents[3] / "migrations"


def build_sync_dsn(db_settings: DatabaseSettings) -> str:
    """Возвращает DSN для синхронного подключения SQLAlchemy."""
    return db_settings.sync_dsn()


def create_engine_from_settings(db_settings: DatabaseSettings | None = None) -> Engine:
    """Создаёт и возвращает SQLAlchemy Engine."""
    settings = db_settings or get_settings().database
    dsn = build_sync_dsn(settings)
    LOGGER.debug("Создание движка SQLAlchemy для %s", dsn)
    return create_engine(dsn, future=True, pool_pre_ping=True)


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Возвращает sessionmaker с автокомитом и автоматическим закрытием."""
    eng = engine or create_engine_from_settings()
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Генерирует контекстный менеджер для безопасной работы с транзакциями."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_schema_migrations_table(engine: Engine) -> None:
    """Создаёт таблицу истории миграций, если она отсутствует."""
    LOGGER.debug("Проверка наличия таблицы schema_migrations.")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id SERIAL PRIMARY KEY,
                filename TEXT UNIQUE NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )


def run_sql_migrations(
    engine: Engine | None = None,
    migrations_path: Path | None = None,
) -> list[str]:
    """Применяет SQL-миграции по порядку и возвращает список применённых файлов."""
    eng = engine or create_engine_from_settings()
    path = migrations_path or DEFAULT_MIGRATIONS_PATH
    if not path.exists():
        raise FileNotFoundError(f"Каталог миграций не найден: {path}")

    _ensure_schema_migrations_table(eng)
    applied: list[str] = []

    sql_files = sorted(f for f in path.glob("*.sql") if f.is_file())
    LOGGER.info("Найдено %d миграций", len(sql_files))

    with eng.begin() as connection:
        for sql_file in sql_files:
            filename = sql_file.name
            already_applied = connection.execute(
                text("SELECT 1 FROM schema_migrations WHERE filename = :filename"),
                {"filename": filename},
            ).scalar()

            if already_applied:
                LOGGER.debug("Миграция %s уже применена, пропускаем.", filename)
                continue

            LOGGER.info("Применяем миграцию %s", filename)
            sql_content = sql_file.read_text(encoding="utf-8")
            connection.exec_driver_sql(sql_content)
            connection.execute(
                text(
                    "INSERT INTO schema_migrations (filename) VALUES (:filename)"
                ),
                {"filename": filename},
            )
            applied.append(filename)

    return applied

