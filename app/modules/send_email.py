"""Очередь отправки писем и фиксация статусов."""

from __future__ import annotations

import json
import logging
import smtplib
import random
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zoneinfo import ZoneInfo

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
    :scheduled_for,
    :sent_at,
    :last_error,
    CAST(:metadata AS JSONB)
)
RETURNING id;
"""

CHECK_OPT_OUT_SQL = """
SELECT 1 FROM opt_out_registry
WHERE LOWER(contact_value) = LOWER(:contact_value)
LIMIT 1;
"""

SELECT_LAST_SCHEDULED_SQL = """
SELECT scheduled_for
FROM outreach_messages
WHERE scheduled_for IS NOT NULL
ORDER BY scheduled_for DESC
LIMIT 1;
"""


UPDATE_OUTREACH_SQL = """
UPDATE outreach_messages
SET status = :status,
    sent_at = :sent_at,
    last_error = :last_error,
    metadata = metadata || CAST(:metadata AS JSONB),
    updated_at = NOW()
WHERE id = :id
RETURNING id;
"""

SEND_WINDOW_START = time(9, 10)
SEND_WINDOW_END = time(19, 45)


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
        self.timezone_name = settings.timezone
        self._tz = ZoneInfo(self.timezone_name)
        self.sending_enabled = getattr(settings, "email_sending_enabled", True)

    def queue(
        self,
        *,
        company_id: str,
        contact_id: Optional[str],
        to_email: str,
        template: EmailTemplate,
        request_payload: Optional[Dict[str, object]] = None,
        scheduled_for: Optional[datetime] = None,
        session: Optional[Session] = None,
    ) -> str:
        """Сохраняет письмо в очереди с пометкой scheduled."""
        if session is not None:
            return self._queue_with_session(
                session,
                company_id,
                contact_id,
                to_email,
                template,
                request_payload,
                scheduled_for,
            )

        with session_scope(self.session_factory) as scoped_session:
            return self._queue_with_session(
                scoped_session,
                company_id,
                contact_id,
                to_email,
                template,
                request_payload,
                scheduled_for,
            )

    def _queue_with_session(
        self,
        session: Session,
        company_id: str,
        contact_id: Optional[str],
        to_email: str,
        template: EmailTemplate,
        request_payload: Optional[Dict[str, object]],
        scheduled_for: Optional[datetime],
    ) -> str:
        metadata = {"to_email": to_email}
        if request_payload is not None:
            metadata["llm_request"] = request_payload
        scheduled_dt = scheduled_for or self._compute_scheduled_for(session=session)
        return self._persist_status(
            session,
            company_id,
            contact_id,
            template,
            status="scheduled",
            scheduled_for=scheduled_dt,
            sent_at=None,
            last_error=None,
            metadata=metadata,
        )

    def deliver(
        self,
        *,
        outreach_id: str,
        company_id: str,
        contact_id: Optional[str],
        to_email: str,
        subject: str,
        body: str,
        session: Optional[Session] = None,
    ) -> str:
        """Отправляет ранее сохранённое письмо и обновляет статус."""
        if not self.sending_enabled:
            LOGGER.debug(
                "Отправка писем отключена настройкой EMAIL_SENDING_ENABLED, письмо %s оставлено в очереди.",
                outreach_id,
            )
            return "disabled"
        if not self._is_within_send_window(datetime.now(timezone.utc).astimezone(self._tz)):
            LOGGER.debug("Вне окна отправки, письмо %s оставлено в статусе scheduled.", outreach_id)
            return "scheduled"
        if session is not None:
            return self._deliver_with_session(session, outreach_id, company_id, contact_id, to_email, subject, body)

        with session_scope(self.session_factory) as scoped_session:
            return self._deliver_with_session(scoped_session, outreach_id, company_id, contact_id, to_email, subject, body)

    def _deliver_with_session(
        self,
        session: Session,
        outreach_id: str,
        company_id: str,
        contact_id: Optional[str],
        to_email: str,
        subject: str,
        body: str,
    ) -> str:
        if self._is_opt_out(session, to_email):
            LOGGER.info("Контакт %s в opt-out, письмо не отправляется.", to_email)
            self._update_status(
                session,
                outreach_id,
                status="skipped",
                sent_at=None,
                last_error="opt_out",
                metadata={"reason": "opt_out"},
            )
            return "skipped"

        message_id = make_msgid(domain=self.settings.host.split(":")[0]) if self.settings.host else make_msgid()
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_email
        msg["To"] = to_email
        msg["Message-ID"] = message_id
        msg.set_content(body)

        try:
            self._deliver(to_email, msg)
            metadata = {"message_id": message_id}
            self._update_status(
                session,
                outreach_id,
                status="sent",
                sent_at=datetime.now(timezone.utc),
                last_error=None,
                metadata=metadata,
            )
            return "sent"
        except smtplib.SMTPException as exc:  # noqa: PERF203
            LOGGER.error("Ошибка отправки письма: %s", exc)
            metadata = {"message_id": message_id}
            self._update_status(
                session,
                outreach_id,
                status="failed",
                sent_at=None,
                last_error=str(exc),
                metadata=metadata,
            )
            return "failed"

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
        scheduled_for: Optional[datetime],
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
            "scheduled_for": scheduled_for,
            "sent_at": sent_at,
            "last_error": last_error,
            "metadata": json.dumps(metadata),
        }

        result = session.execute(text(INSERT_OUTREACH_SQL), payload)
        return str(result.scalar_one())

    def _update_status(
        self,
        session: Session,
        outreach_id: str,
        *,
        status: str,
        sent_at: Optional[datetime],
        last_error: Optional[str],
        metadata: Dict[str, object],
    ) -> str:
        payload = {
            "id": outreach_id,
            "status": status,
            "sent_at": sent_at,
            "last_error": last_error,
            "metadata": json.dumps(metadata),
        }
        result = session.execute(text(UPDATE_OUTREACH_SQL), payload)
        return str(result.scalar_one())

    def mark_status(
        self,
        *,
        outreach_id: str,
        status: str,
        sent_at: Optional[datetime] = None,
        last_error: Optional[str] = None,
        metadata: Optional[Dict[str, object]] = None,
        session: Optional[Session] = None,
    ) -> str:
        """Проставляет произвольный статус для записи рассылки."""
        metadata_payload = metadata or {}
        if session is not None:
            return self._update_status(
                session,
                outreach_id,
                status=status,
                sent_at=sent_at,
                last_error=last_error,
                metadata=metadata_payload,
            )

        with session_scope(self.session_factory) as scoped_session:
            return self._update_status(
                scoped_session,
                outreach_id,
                status=status,
                sent_at=sent_at,
                last_error=last_error,
                metadata=metadata_payload,
            )

    def _compute_scheduled_for(
        self,
        *,
        session: Session,
        reference: Optional[datetime] = None,
    ) -> datetime:
        now_utc = reference or datetime.now(timezone.utc)
        local_now = now_utc.astimezone(self._tz)

        last_scheduled = session.execute(text(SELECT_LAST_SCHEDULED_SQL)).scalar_one_or_none()
        if last_scheduled:
            last_local = last_scheduled.astimezone(self._tz)
            anchor = last_local if last_local > local_now else local_now
        else:
            anchor = local_now

        delay_seconds = random.randint(240, 480)

        scheduled_local = self._pick_time_within_window(anchor, delay_seconds)
        return scheduled_local.astimezone(timezone.utc)

    def _pick_time_within_window(self, anchor_local: datetime, delay_seconds: int) -> datetime:
        window_start = datetime.combine(anchor_local.date(), SEND_WINDOW_START, tzinfo=self._tz)
        window_end = datetime.combine(anchor_local.date(), SEND_WINDOW_END, tzinfo=self._tz)

        if anchor_local < window_start:
            base = window_start
        elif anchor_local > window_end:
            next_day = anchor_local.date() + timedelta(days=1)
            base = datetime.combine(next_day, SEND_WINDOW_START, tzinfo=self._tz)
            window_end = datetime.combine(next_day, SEND_WINDOW_END, tzinfo=self._tz)
        else:
            base = anchor_local

        candidate = base + timedelta(seconds=delay_seconds)
        if candidate > window_end:
            next_day = base.date() + timedelta(days=1)
            base = datetime.combine(next_day, SEND_WINDOW_START, tzinfo=self._tz)
            window_end = datetime.combine(next_day, SEND_WINDOW_END, tzinfo=self._tz)
            candidate = base + timedelta(seconds=random.randint(240, 480))

        return candidate

    def _is_within_send_window(self, local_dt: datetime) -> bool:
        start = datetime.combine(local_dt.date(), SEND_WINDOW_START, tzinfo=self._tz)
        end = datetime.combine(local_dt.date(), SEND_WINDOW_END, tzinfo=self._tz)
        return start <= local_dt <= end

    def is_within_send_window(self, *, reference: Optional[datetime] = None) -> bool:
        base = reference or datetime.now(timezone.utc)
        local_dt = base.astimezone(self._tz)
        return self._is_within_send_window(local_dt)
