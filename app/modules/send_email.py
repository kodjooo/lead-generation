"""Отправка писем и фиксация статусов."""

from __future__ import annotations

import json
import logging
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.config import SMTPSettings, get_settings
from app.modules.generate_email_gpt import EmailTemplate
from app.modules.utils.db import get_session_factory, session_scope

LOGGER = logging.getLogger("app.send_email")


INSERT_OUTREACH_SQL = """
INSERT INTO outreach_messages (
    company_id,
    contact_id,
    channel,
    subject,
    body,
    status,
    scheduled_for,
    sent_at,
    last_error,
    metadata
)
VALUES (
    :company_id,
    :contact_id,
    'email',
    :subject,
    :body,
    :status,
    NULL,
    :sent_at,
    :last_error,
    :metadata::jsonb
)
RETURNING id;
"""

CHECK_OPT_OUT_SQL = """
SELECT 1 FROM opt_out_registry
WHERE LOWER(contact_value) = LOWER(:contact_value)
LIMIT 1;
"""


class EmailSender:
    """Отвечает за доставку писем и фиксацию статусов в БД."""

    def __init__(
        self,
        *,
        session_factory: Optional[sessionmaker[Session]] = None,
        smtp_settings: Optional[SMTPSettings] = None,
        use_starttls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        settings = get_settings()
        self.settings = smtp_settings or settings.smtp
        self.from_email = self.settings.sender or settings.smtp.sender or "leadgen@example.com"
        self.session_factory = session_factory or get_session_factory()
        self.use_starttls = use_starttls
        self.timeout = timeout

    def send(
        self,
        *,
        company_id: str,
        contact_id: Optional[str],
        to_email: str,
        template: EmailTemplate,
        session: Optional[Session] = None,
    ) -> str:
        """Отправляет письмо и возвращает идентификатор записи outreach."""
        if session is not None:
            return self._send_with_session(session, company_id, contact_id, to_email, template)

        with session_scope(self.session_factory) as scoped_session:
            return self._send_with_session(scoped_session, company_id, contact_id, to_email, template)

    def _send_with_session(
        self,
        session: Session,
        company_id: str,
        contact_id: Optional[str],
        to_email: str,
        template: EmailTemplate,
    ) -> str:
        if self._is_opt_out(session, to_email):
            LOGGER.info("Контакт %s в opt-out, письмо не отправляется.", to_email)
            return self._persist_status(
                session,
                company_id,
                contact_id,
                template,
                status="skipped",
                sent_at=None,
                last_error="opt_out",
                metadata={"reason": "opt_out"},
            )

        message_id = make_msgid(domain=self.settings.host.split(":")[0]) if self.settings.host else make_msgid()
        msg = EmailMessage()
        msg["Subject"] = template.subject
        msg["From"] = self.from_email
        msg["To"] = to_email
        msg["Message-ID"] = message_id
        msg.set_content(template.body)

        try:
            self._deliver(to_email, msg)
            metadata = {"message_id": message_id}
            return self._persist_status(
                session,
                company_id,
                contact_id,
                template,
                status="sent",
                sent_at=datetime.now(timezone.utc),
                last_error=None,
                metadata=metadata,
            )
        except smtplib.SMTPException as exc:  # noqa: PERF203
            LOGGER.error("Ошибка отправки письма: %s", exc)
            metadata = {"message_id": message_id}
            return self._persist_status(
                session,
                company_id,
                contact_id,
                template,
                status="failed",
                sent_at=None,
                last_error=str(exc),
                metadata=metadata,
            )

    def _deliver(self, to_email: str, message: EmailMessage) -> None:
        LOGGER.debug("Отправка письма %s -> %s", message["Message-ID"], to_email)
        with smtplib.SMTP(self.settings.host, self.settings.port, timeout=self.timeout) as smtp:
            if self.use_starttls:
                smtp.starttls()
            if self.settings.username and self.settings.password:
                smtp.login(self.settings.username, self.settings.password)
            smtp.send_message(message)

    def _is_opt_out(self, session: Session, to_email: str) -> bool:
        result = session.execute(text(CHECK_OPT_OUT_SQL), {"contact_value": to_email})
        return result.first() is not None

    def _persist_status(
        self,
        session: Session,
        company_id: str,
        contact_id: Optional[str],
        template: EmailTemplate,
        *,
        status: str,
        sent_at: Optional[datetime],
        last_error: Optional[str],
        metadata: Dict[str, object],
    ) -> str:
        payload = {
            "company_id": company_id,
            "contact_id": contact_id,
            "subject": template.subject,
            "body": template.body,
            "status": status,
            "sent_at": sent_at,
            "last_error": last_error,
            "metadata": json.dumps(metadata),
        }

        result = session.execute(text(INSERT_OUTREACH_SQL), payload)
        return str(result.scalar_one())
