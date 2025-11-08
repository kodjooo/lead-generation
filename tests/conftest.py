"""Общие фикстуры для тестов."""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Заполняет обязательные переменные окружения значениями по умолчанию."""
    monkeypatch.setenv("YANDEX_CLOUD_IAM_TOKEN", "test-token")
    monkeypatch.setenv("YANDEX_CLOUD_FOLDER_ID", "test-folder")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("SMTP_USERNAME", "test-smtp")
    monkeypatch.setenv("SMTP_PASSWORD", "test-smtp-password")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "leadgen@example.com")
    monkeypatch.setenv("GMAIL_USER", "test-smtp")
    monkeypatch.setenv("GMAIL_PASS", "test-smtp-password")
    monkeypatch.setenv("GMAIL_FROM", "Lead Gen <leadgen@example.com>")
    monkeypatch.setenv("GMAIL_SMTP_TLS", "true")
    monkeypatch.setenv("ROUTING_ENABLED", "false")


@pytest.fixture
def sample_companies() -> List[Dict[str, Any]]:
    """Возвращает список тестовых компаний из фикстуры JSON."""
    path = FIXTURES_DIR / "sample_companies.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)
