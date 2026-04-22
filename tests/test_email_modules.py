"""Тесты генерации и отправки писем."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from app.config import get_settings
from app.modules.generate_email_gpt import CompanyBrief, EmailGenerationError, EmailGenerator, EmailTemplate, OfferBrief
from app.modules.send_email import EmailSender
from app.modules.mx_router import MXResult


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


class DummyUpdateResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one(self) -> str:
        return self._value

    def first(self) -> Any:
        return (self._value,)


class DummyScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class DummySession:
    def __init__(self, opt_out_emails: Optional[List[str]] = None) -> None:
        self.opt_out_emails = {email.lower() for email in (opt_out_emails or [])}
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def execute(self, statement, params=None):  # noqa: ANN001
        sql = statement.text if hasattr(statement, "text") else str(statement)
        params = params or {}
        self.calls.append((sql.strip(), params))

        if "SELECT scheduled_for" in sql and "FROM outreach_messages" in sql:
            last = None
            for recorded_sql, recorded_params in reversed(self.calls[:-1]):
                if "INSERT INTO outreach_messages" in recorded_sql:
                    last = recorded_params.get("scheduled_for")
                    if last is not None:
                        break
            return DummyScalarResult(last)

        if "FROM opt_out_registry" in sql:
            email = params.get("contact_value", "").lower()
            rows = [(1,)] if email in self.opt_out_emails else []
            return DummySelectResult(rows)

        if "INSERT INTO outreach_messages" in sql:
            idx = len([c for c in self.calls if "INSERT INTO outreach_messages" in c[0]])
            return DummyInsertResult(f"outreach-{idx}")

        if "UPDATE outreach_messages" in sql:
            return DummyUpdateResult(params.get("id", "outreach-update"))

        raise AssertionError(f"Unexpected SQL: {sql}")

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def reset_settings_cache() -> None:
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_email_generator_raises_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("OPENAI_API_KEY", "")

    generator = EmailGenerator()
    company = CompanyBrief(name="Test", domain="test.ru")
    offer = OfferBrief(pains=["Долгий поиск лидов"], value_proposition="Автоматизируем холодный аутрич")

    with pytest.raises(EmailGenerationError, match="OPENAI_API_KEY"):
        generator.generate(company, offer)

    reset_settings_cache()


@respx.mock
def test_email_generator_retries_and_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMAIL_MODEL", "gpt-5-2025-08-07")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")

    respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(500, json={"error": {"message": "temporary"}})
    )

    generator = EmailGenerator(retry_attempts=2)
    company = CompanyBrief(name="Alpha", domain="alpha.ru", industry="Маркетинг")
    offer = OfferBrief(pains=["Нужны лиды"], value_proposition="Запустим кампанию за 7 дней")
    sleeps: List[int] = []
    monkeypatch.setattr("app.modules.generate_email_gpt.time.sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(EmailGenerationError, match="Не удалось сгенерировать письмо"):
        generator.generate(company, offer)

    assert respx.calls.call_count == 2
    assert sleeps == [20]

    reset_settings_cache()


def test_email_generator_uses_progressive_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMAIL_MODEL", "gpt-5-2025-08-07")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")

    call_count = {"value": 0}

    def fake_request(payload):  # noqa: ANN001
        call_count["value"] += 1
        raise httpx.HTTPError("temporary")

    sleeps: List[int] = []
    monkeypatch.setattr("app.modules.generate_email_gpt.EmailGenerator._request_openai", lambda self, payload: fake_request(payload))
    monkeypatch.setattr("app.modules.generate_email_gpt.time.sleep", lambda seconds: sleeps.append(seconds))

    generator = EmailGenerator(retry_attempts=3)
    company = CompanyBrief(name="Alpha", domain="alpha.ru", industry="Маркетинг")
    offer = OfferBrief(pains=["Нужны лиды"], value_proposition="Запустим кампанию за 7 дней")

    with pytest.raises(EmailGenerationError):
        generator.generate(company, offer)

    assert call_count["value"] == 3
    assert sleeps == [20, 40]

    reset_settings_cache()


@respx.mock
def test_email_generator_calls_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_settings_cache()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMAIL_MODEL", "gpt-5-2025-08-07")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")

    response_json = {
        "output_text": json.dumps({"subject": "Тема", "body": "Текст"})
    }
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(200, json=response_json)
    )

    generator = EmailGenerator()
    company = CompanyBrief(name="Alpha", domain="alpha.ru", industry="Маркетинг")
    offer = OfferBrief(pains=["Нужны лиды"], value_proposition="Запустим кампанию за 7 дней")

    generated = generator.generate(company, offer)

    assert generated.template.subject == "Тема"
    assert generated.template.body == "Текст"
    assert generated.request_payload is not None
    assert generated.used_fallback is False
    assert generated.request_payload["model"] == "gpt-5-2025-08-07"
    assert generated.request_payload["reasoning"] == {"effort": "low"}
    assert route.called

    reset_settings_cache()


def test_email_sender_queue_persists_email(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    template = generator_template()
    monkeypatch.setattr(
        sender,
        "_compute_scheduled_for",
        lambda session, reference=None: datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc),
    )

    outreach_id = sender.queue(
        company_id="c1",
        contact_id="contact1",
        to_email="hello@example.com",
        template=template,
        request_payload={"messages": []},
        session=session,
    )

    assert outreach_id == "outreach-1"
    sql, params = session.calls[-1]
    assert "INSERT INTO outreach_messages" in sql
    assert params["status"] == "scheduled"
    metadata = json.loads(params["metadata"])
    assert metadata["to_email"] == "hello@example.com"
    assert metadata["llm_request"] == {"messages": []}

    reset_settings_cache()


def test_email_sender_queue_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()


def test_email_sender_queue_skips_invalid_email(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    template = generator_template()

    outreach_id = sender.queue(
        company_id="c1",
        contact_id="contact1",
        to_email="+74951234567",
        template=template,
        request_payload=None,
        session=session,
    )

    assert outreach_id == "outreach-1"
    sql, params = session.calls[-1]
    assert "INSERT INTO outreach_messages" in sql
    assert params["status"] == "skipped"
    assert params["last_error"] == "invalid_email"
    metadata = json.loads(params["metadata"])
    assert metadata["reason"] == "invalid_email"
    assert metadata["to_email_raw"] == "+74951234567"

    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    template = generator_template()

    delays = iter([240, 300])
    monkeypatch.setattr("app.modules.send_email.random.randint", lambda a, b: next(delays))

    class FixedDatetime(datetime):
        _values = iter([])

        @classmethod
        def now(cls, tz=None):
            value = next(cls._values)
            if tz is not None:
                return value.astimezone(tz)
            return value

    FixedDatetime._values = iter(
        [
            datetime(2025, 10, 24, 6, 0, 0, tzinfo=timezone.utc),
            datetime(2025, 10, 24, 6, 0, 5, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr("app.modules.send_email.datetime", FixedDatetime)

    sender.queue(
        company_id="c1",
        contact_id="contact1",
        to_email="first@example.com",
        template=template,
        request_payload=None,
        session=session,
    )
    sender.queue(
        company_id="c2",
        contact_id="contact2",
        to_email="second@example.com",
        template=template,
        request_payload=None,
        session=session,
    )

    scheduled = [
        params["scheduled_for"]
        for sql, params in session.calls
        if "INSERT INTO outreach_messages" in sql
    ]
    assert len(scheduled) >= 2
    diff_seconds = (scheduled[-1] - scheduled[-2]).total_seconds()
    assert diff_seconds == pytest.approx(300.0, abs=1.0)


def test_email_sender_marks_failed_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    monkeypatch.setattr(sender, "_prepare_route", lambda email: MagicMock(provider="gmail", channel=MagicMock(), mx_result=MXResult("OTHER", [], False), reply_to=None, fallback=False))
    monkeypatch.setattr(sender, "_make_message_id", lambda channel: "<test-message-id>")
    monkeypatch.setattr(sender, "_apply_headers", lambda message, channel, reply_to=None: None)
    monkeypatch.setattr(sender, "_send_via_channel", lambda to_email, message, channel: (_ for _ in ()).throw(OSError(101, "Network is unreachable")))

    result = sender.deliver(
        outreach_id="outreach-1",
        company_id="company-1",
        contact_id="contact-1",
        to_email="hello@example.com",
        subject="Тест",
        body="Тестовое письмо",
        session=session,
    )

    assert result == "failed"
    sql, params = session.calls[-1]
    assert "UPDATE outreach_messages" in sql
    assert params["status"] == "failed"
    assert "Network is unreachable" in params["last_error"]

    reset_settings_cache()

    reset_settings_cache()


def test_email_sender_deliver_skips_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession(opt_out_emails=["skip@example.com"])
    reset_settings_cache()
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    sender.mx_router = MagicMock()
    sender.mx_router.classify.return_value = MXResult("OTHER", [], False)
    monkeypatch.setattr(sender, "_send_via_channel", MagicMock(side_effect=AssertionError("deliver must not be called")))
    monkeypatch.setattr(
        sender,
        "_compute_scheduled_for",
        lambda session, reference=None: datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(sender, "_is_within_send_window", lambda *_: True)

    template = generator_template()
    outreach_id = sender.queue(
        company_id="c1",
        contact_id="contact1",
        to_email="skip@example.com",
        template=template,
        request_payload={"messages": []},
        session=session,
    )

    result = sender.deliver(
        outreach_id=outreach_id,
        company_id="c1",
        contact_id="contact1",
        to_email="skip@example.com",
        subject=template.subject,
        body=template.body,
        session=session,
    )

    assert result == "skipped"
    sql, params = session.calls[-1]
    assert "UPDATE outreach_messages" in sql
    assert params["status"] == "skipped"
    assert params["last_error"] == "opt_out"

    reset_settings_cache()


def test_email_sender_deliver_skips_invalid_email(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    sender.mx_router = MagicMock()
    sender.mx_router.classify.return_value = MXResult("OTHER", [], False)
    monkeypatch.setattr(sender, "_send_via_channel", MagicMock())
    monkeypatch.setattr(sender, "_is_within_send_window", lambda *_: True)

    template = generator_template()
    outreach_id = sender.queue(
        company_id="c1",
        contact_id="contact1",
        to_email="info@example.com",
        template=template,
        request_payload=None,
        session=session,
    )

    result = sender.deliver(
        outreach_id=outreach_id,
        company_id="c1",
        contact_id="contact1",
        to_email="not-an-email",
        subject=template.subject,
        body=template.body,
        session=session,
    )

    assert result == "skipped"
    sql, params = session.calls[-1]
    assert "UPDATE outreach_messages" in sql
    assert params["status"] == "skipped"
    assert params["last_error"] == "invalid_email"

    reset_settings_cache()


def test_email_sender_deliver_success(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    sender.mx_router = MagicMock()
    sender.mx_router.classify.return_value = MXResult("OTHER", ["mx.test"], False)
    deliver_mock = MagicMock()
    monkeypatch.setattr(sender, "_send_via_channel", deliver_mock)
    monkeypatch.setattr(
        sender,
        "_compute_scheduled_for",
        lambda session, reference=None: datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(sender, "_is_within_send_window", lambda *_: True)

    template = generator_template()
    outreach_id = sender.queue(
        company_id="c1",
        contact_id="contact1",
        to_email="hello@example.com",
        template=template,
        request_payload={"messages": []},
        session=session,
    )

    result = sender.deliver(
        outreach_id=outreach_id,
        company_id="c1",
        contact_id="contact1",
        to_email="hello@example.com",
        subject=template.subject,
        body=template.body,
        session=session,
    )

    assert result == "sent"
    deliver_mock.assert_called_once()

    sql, params = session.calls[-1]
    assert params["status"] == "sent"
    assert isinstance(params["sent_at"], datetime)
    assert params["last_error"] is None

    reset_settings_cache()


def test_email_sender_deliver_rejects_repeat_send(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    reset_settings_cache()

    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    sender.mx_router = MagicMock()
    sender.mx_router.classify.return_value = MXResult("OTHER", ["mx.test"], False)
    deliver_mock = MagicMock()
    monkeypatch.setattr(sender, "_send_via_channel", deliver_mock)
    monkeypatch.setattr(sender, "_is_within_send_window", lambda *_: True)

    template = generator_template()
    outreach_id = sender.queue(
        company_id="c1",
        contact_id="contact1",
        to_email="hello@example.com",
        template=template,
        request_payload={"messages": []},
        session=session,
    )

    first = sender.deliver(
        outreach_id=outreach_id,
        company_id="c1",
        contact_id="contact1",
        to_email="hello@example.com",
        subject=template.subject,
        body=template.body,
        session=session,
    )
    second = sender.deliver(
        outreach_id=outreach_id,
        company_id="c1",
        contact_id="contact1",
        to_email="hello@example.com",
        subject=template.subject,
        body=template.body,
        session=session,
    )

    assert first == "sent"
    assert second == "skipped"
    assert deliver_mock.call_count == 1

    reset_settings_cache()


def test_email_sender_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()

    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "false")
    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    sender.mx_router = MagicMock()
    sender.mx_router.classify.return_value = MXResult("OTHER", ["mx.test"], False)
    deliver_mock = MagicMock()
    monkeypatch.setattr(sender, "_send_via_channel", deliver_mock)
    monkeypatch.setattr(sender, "_is_within_send_window", lambda *_: True)

    result = sender.deliver(
        outreach_id="outreach-test",
        company_id="c1",
        contact_id="contact1",
        to_email="hello@example.com",
        subject="Тема",
        body="Текст",
        session=session,
    )

    assert result == "disabled"
    deliver_mock.assert_not_called()

    monkeypatch.delenv("EMAIL_SENDING_ENABLED", raising=False)
    reset_settings_cache()


def generator_template():
    return EmailTemplate(subject="Тестовая тема", body="Тестовое тело письма")
