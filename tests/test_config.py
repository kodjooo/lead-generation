"""Тесты конфигурации приложения."""

from app.config import get_settings


def test_settings_loaded_from_env(monkeypatch) -> None:
    """Проверяет, что настройки корректно читаются из окружения."""
    get_settings.cache_clear()  # type: ignore[attr-defined]

    monkeypatch.setenv("POSTGRES_HOST", "db-test")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_USER", "tester")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("POSTGRES_DB", "leadgen_test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_PORT", "2525")

    settings = get_settings()

    assert settings.database.host == "db-test"
    assert settings.database.port == 5433
    assert settings.database.user == "tester"
    assert settings.database.password == "secret"
    assert settings.database.name == "leadgen_test"
    assert settings.redis_url == "redis://localhost:6379/1"
    assert settings.smtp.host == "smtp.test"
    assert settings.smtp.port == 2525
    assert settings.smtp.username == "test-smtp"
    assert settings.smtp.password == "test-smtp-password"
    assert settings.smtp.sender == "leadgen@example.com"

    get_settings.cache_clear()  # type: ignore[attr-defined]
