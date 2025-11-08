"""Тесты маршрутизации SMTP при отправке писем."""

from __future__ import annotations

import json
import smtplib
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from app.config import get_settings
from app.modules.mx_router import MXResult
from app.modules.send_email import EmailSender
from tests.test_email_modules import DummySession, generator_template, reset_settings_cache


def setup_yandex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTING_ENABLED", "true")
    monkeypatch.setenv("YANDEX_SMTP_HOST", "smtp.yandex.test")
    monkeypatch.setenv("YANDEX_SMTP_PORT", "465")
    monkeypatch.setenv("YANDEX_USER", "sender@yandex.ru")
    monkeypatch.setenv("YANDEX_PASS", "yandex-pass")
    monkeypatch.setenv("YANDEX_FROM", "Yandex Sender <sender@yandex.ru>")
    monkeypatch.setenv("GMAIL_FROM", "Gmail Sender <leadgen@example.com>")


def parse_last_update(session: DummySession) -> Tuple[str, Dict[str, Any]]:
    sql, params = session.calls[-1]
    assert "UPDATE outreach_messages" in sql
    payload = json.loads(params["metadata"])
    return params["status"], payload


def prepare_sender(monkeypatch: pytest.MonkeyPatch, session: DummySession) -> EmailSender:
    sender = EmailSender(session_factory=lambda: session, use_starttls=False)  # type: ignore[arg-type]
    sender.mx_router = MagicMock()
    monkeypatch.setattr(sender, "_is_within_send_window", lambda *_: True)
    return sender


def deliver_email(sender: EmailSender, session: DummySession, *, to_email: str) -> str:
    template = generator_template()
    outreach_id = sender.queue(
        company_id="c1",
        contact_id="contact1",
        to_email=to_email,
        template=template,
        request_payload=None,
        session=session,
    )
    return sender.deliver(
        outreach_id=outreach_id,
        company_id="c1",
        contact_id="contact1",
        to_email=to_email,
        subject=template.subject,
        body=template.body,
        session=session,
    )


def test_ru_classification_routes_to_yandex(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()
    setup_yandex_env(monkeypatch)

    sender = prepare_sender(monkeypatch, session)
    sender.mx_router.classify.return_value = MXResult("RU", ["mx.yandex.net"], False)

    send_mock = MagicMock()
    monkeypatch.setattr(sender, "_send_via_channel", send_mock)

    result = deliver_email(sender, session, to_email="lead@yandex.ru")

    assert result == "sent"
    args = send_mock.call_args[0]
    assert args[2] == sender.yandex_settings
    message = args[1]
    assert message["Reply-To"] == "Gmail Sender <leadgen@example.com>"

    status, metadata = parse_last_update(session)
    assert status == "sent"
    assert metadata["route"]["provider"] == "yandex"
    assert metadata["mx"]["class"] == "RU"

    reset_settings_cache()


def test_other_classification_routes_to_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()

    sender = prepare_sender(monkeypatch, session)
    sender.mx_router.classify.return_value = MXResult("OTHER", ["aspmx.l.google.com"], False)

    send_mock = MagicMock()
    monkeypatch.setattr(sender, "_send_via_channel", send_mock)

    result = deliver_email(sender, session, to_email="hello@gmail.com")

    assert result == "sent"
    args = send_mock.call_args[0]
    assert args[2] == sender.gmail_settings
    message = args[1]
    assert message["Reply-To"] is None

    status, metadata = parse_last_update(session)
    assert metadata["route"]["provider"] == "gmail"
    assert metadata["route"]["fallback"] is False

    reset_settings_cache()


def test_unknown_classification_defaults_to_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()

    sender = prepare_sender(monkeypatch, session)
    sender.mx_router.classify.return_value = MXResult("UNKNOWN", [], False)

    send_mock = MagicMock()
    monkeypatch.setattr(sender, "_send_via_channel", send_mock)

    result = deliver_email(sender, session, to_email="timeout@example.com")

    assert result == "sent"
    status, metadata = parse_last_update(session)
    assert metadata["mx"]["class"] == "UNKNOWN"
    assert metadata["route"]["provider"] == "gmail"

    reset_settings_cache()


def test_yandex_auth_failure_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DummySession()
    reset_settings_cache()
    setup_yandex_env(monkeypatch)

    sender = prepare_sender(monkeypatch, session)
    sender.mx_router.classify.return_value = MXResult("RU", ["mx.yandex.net"], False)

    def send_side_effect(to_email, message, channel):  # noqa: ANN001
        if channel == sender.yandex_settings:
            raise smtplib.SMTPAuthenticationError(535, b"Auth failed")
        return None

    send_mock = MagicMock(side_effect=send_side_effect)
    monkeypatch.setattr(sender, "_send_via_channel", send_mock)

    result = deliver_email(sender, session, to_email="lead@yandex.ru")

    assert result == "failed"
    assert send_mock.call_count == 1
    status, metadata = parse_last_update(session)
    assert status == "failed"
    assert metadata["route"]["provider"] == "yandex"
    assert metadata["route"]["fallback"] is False
    assert "Auth failed" in metadata["route"]["error"]

    reset_settings_cache()
