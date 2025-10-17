"""Тесты генерации и отправки писем."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from app.config import get_settings
from app.modules.generate_email_gpt import CompanyBrief, EmailGenerator, OfferBrief
from app.modules.send_email import EmailSender


class DummySelectResult:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class DummyInsertResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one(self) -> str:
        return self._value


class DummySession:
    def __init__(self, opt_out_emails: Optional[List[str]] = None) -> None:
        self.opt_out_emails = {email.lower() for email in (opt_out_emails or [])}
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def execute(self, statement, params=None):  # noqa: ANN001
        sql = statement.text if hasattr(statement, "text") else str(statement)
        params = params or {}
        self.calls.append((sql.strip(), params))

        if "FROM opt_out_registry" in sql:
            email = params.get("contact_value", "").lower()
            rows = [(1,)] if email in self.opt_out_emails else []
            return DummySelectResult(rows)

        if "INSERT INTO outreach_messages" in sql:
            idx = len([c for c in self.calls if "INSERT INTO outreach_messages" in c[0]])
            return DummyInsertResult(f"outreach-{idx}")

        raise AssertionError(f"Unexpected SQL: {sql}")

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def reset_settings_cache() -> None:
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_email_generator_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("OPENAI_API_KEY", "")

    generator = EmailGenerator()
    company = CompanyBrief(name="Test", domain="test.ru")
    offer = OfferBrief(pains=["Долгий поиск лидов"], value_proposition="Автоматизируем холодный аутрич")

    template = generator.generate(company, offer)

    assert "LeadGen" in template.body

    reset_settings_cache()


@respx.mock
def test_email_generator_calls_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    response_json = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"subject": "Тема", "body": "Текст"})
                }
            }
        ]
    }
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=response_json)
    )

    generator = EmailGenerator()
    company = CompanyBrief(name="Alpha", domain="alpha.ru", industry="Маркетинг")
    offer = OfferBrief(pains=["Нужны лиды"], value_proposition="Запустим кампанию за 7 дней")

    template = generator.generate(company, offer)

    assert template.subject == "Тема"
    assert template.body == "Текст"

    reset_settings_cache()


def test_email_sender_skips_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession(opt_out_emails=["skip@example.com"])
    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    monkeypatch.setattr(sender, "_deliver", MagicMock(side_effect=AssertionError("deliver must not be called")))

    template = generator_template()
    outreach_id = sender.send(
        company_id="c1",
        contact_id="contact1",
        to_email="skip@example.com",
        template=template,
        session=session,
    )

    assert outreach_id == "outreach-1"
    sql, params = session.calls[-1]
    assert params["status"] == "skipped"
    assert params["last_error"] == "opt_out"

    reset_settings_cache()


def test_email_sender_success(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    deliver_mock = MagicMock()
    monkeypatch.setattr(sender, "_deliver", deliver_mock)

    template = generator_template()
    outreach_id = sender.send(
        company_id="c1",
        contact_id="contact1",
        to_email="hello@example.com",
        template=template,
        session=session,
    )

    assert outreach_id == "outreach-1"
    deliver_mock.assert_called_once()

    sql, params = session.calls[-1]
    assert params["status"] == "sent"
    assert isinstance(params["sent_at"], datetime)
    assert params["last_error"] is None

    reset_settings_cache()


def generator_template():
    company = CompanyBrief(name="Test", domain="test.ru")
    offer = OfferBrief(value_proposition="Automation")
    generator = EmailGenerator()
    return generator._fallback_template(company, offer, None)
