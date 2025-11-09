"""Очередь отправки писем и фиксация статусов."""

from __future__ import annotations

import json
import logging
import random
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from zoneinfo import ZoneInfo

from app.config import SMTPChannelSettings, get_settings
from app.modules.generate_email_gpt import EmailTemplate
from app.modules.mx_router import MXResult, MXRouter
from app.modules.utils.db import get_session_factory, session_scope
from app.modules.utils.email import clean_email, is_valid_email

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
WHERE channel = 'email'
  AND scheduled_for IS NOT NULL
ORDER BY scheduled_for DESC
LIMIT 1
FOR UPDATE SKIP LOCKED;
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
MIN_SEND_DELAY_SECONDS = 9 * 60
MAX_SEND_DELAY_SECONDS = 16 * 60


@dataclass
class RouteContext:
    """Содержит информацию о выбранном канале отправки."""

    provider: str
    channel: SMTPChannelSettings
    mx_result: MXResult
    reply_to: Optional[str]
    fallback: bool = False


def _mask_email(value: str) -> str:
    """Маскирует контакт для логов."""
    if "@" not in value:
        return value
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        masked = local[0] + "*" * max(len(local) - 1, 0)
    else:
        masked = f"{local[:2]}***"
    return f"{masked}@{domain}"


class EmailSender:
    """Отвечает за доставку писем и фиксацию статусов в БД."""

    def __init__(
        self,
        *,
        session_factory: Optional[sessionmaker[Session]] = None,
        smtp_settings: Optional[SMTPChannelSettings] = None,
        mx_router: Optional[MXRouter] = None,
        use_starttls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        settings = get_settings()
        self.gmail_settings = settings.smtp_gmail
        self.yandex_settings = settings.smtp_yandex
        self.routing_settings = settings.routing
        self.default_channel = smtp_settings or self.gmail_settings
        self.mx_router = mx_router or MXRouter(self.routing_settings)
        self.gmail_from_header = self._build_from_header(self.gmail_settings)
        self.session_factory = session_factory or get_session_factory()
        self.use_starttls = use_starttls
        self.timeout = timeout
        self.timezone_name = settings.timezone
        self._tz = ZoneInfo(self.timezone_name)
        self.sending_enabled = getattr(settings, "email_sending_enabled", True)

    def _build_from_header(self, channel: SMTPChannelSettings) -> str:
        """Формирует заголовок From с учётом имени отправителя."""
        raw_sender = (channel.sender or "").strip()
        sender_name = (channel.sender_name or "").strip() if channel.sender_name else ""

        if sender_name and raw_sender:
            return formataddr((sender_name, raw_sender))

        name_from_value, email_from_value = parseaddr(raw_sender)
        if name_from_value and email_from_value:
            return formataddr((name_from_value, email_from_value))

        if raw_sender:
            return raw_sender

        fallback = "leadgen@example.com"
        if sender_name:
            return formataddr((sender_name, fallback))
        return fallback

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
        normalized_email = clean_email(to_email)
        metadata: Dict[str, object] = {
            "to_email": normalized_email or to_email,
            "to_email_raw": to_email,
        }
        if request_payload is not None:
            metadata["llm_request"] = request_payload

        if not is_valid_email(normalized_email):
            metadata["reason"] = "invalid_email"
            LOGGER.warning(
                "Email %s не прошёл валидацию, запись будет помечена как skipped.",
                to_email,
            )
            return self._persist_status(
                session,
                company_id,
                contact_id,
                template,
                status="skipped",
                scheduled_for=None,
                sent_at=None,
                last_error="invalid_email",
                metadata=metadata,
            )

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
        normalized_email = clean_email(to_email)
        if not is_valid_email(normalized_email):
            LOGGER.warning(
                "Outreach %s пропущен — email '%s' не проходит валидацию.",
                outreach_id,
                to_email,
            )
            self._update_status(
                session,
                outreach_id,
                status="skipped",
                sent_at=None,
                last_error="invalid_email",
                metadata={
                    "reason": "invalid_email",
                    "to_email": normalized_email or to_email,
                    "to_email_raw": to_email,
                },
            )
            return "skipped"

        if self._is_opt_out(session, normalized_email):
            LOGGER.info("Контакт %s в opt-out, письмо не отправляется.", _mask_email(normalized_email))
            self._update_status(
                session,
                outreach_id,
                status="skipped",
                sent_at=None,
                last_error="opt_out",
                metadata={"reason": "opt_out"},
            )
            return "skipped"

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["To"] = normalized_email
        msg.set_content(body)

        route = self._prepare_route(normalized_email)
        message_id = self._make_message_id(route.channel)
        msg["Message-ID"] = message_id
        self._apply_headers(msg, route.channel, reply_to=route.reply_to)
        checked_at = datetime.now(timezone.utc)

        metadata: Dict[str, object] = {
            "message_id": message_id,
            "recipient": normalized_email,
            "mx": {
                "class": route.mx_result.classification,
                "records": route.mx_result.records,
                "checked_at": checked_at.isoformat(),
            },
            "route": {
                "provider": route.provider,
                "fallback": route.fallback,
            },
        }

        try:
            route = self._deliver_with_fallback(normalized_email, msg, route, metadata)
            self._update_status(
                session,
                outreach_id,
                status="sent",
                sent_at=datetime.now(timezone.utc),
                last_error=None,
                metadata=metadata,
            )
            LOGGER.info(
                "Письмо %s отправлено через %s (mx=%s).",
                _mask_email(normalized_email),
                metadata["route"]["provider"],
                route.mx_result.classification,
            )
            return "sent"
        except smtplib.SMTPAuthenticationError as exc:
            LOGGER.error("Ошибка авторизации SMTP (%s): %s", _mask_email(normalized_email), exc)
            metadata["route"]["error"] = str(exc)
            self._update_status(
                session,
                outreach_id,
                status="failed",
                sent_at=None,
                last_error=str(exc),
                metadata=metadata,
            )
            return "failed"
        except smtplib.SMTPException as exc:  # noqa: PERF203
            metadata["route"]["error"] = str(exc)
            self._update_status(
                session,
                outreach_id,
                status="failed",
                sent_at=None,
                last_error=str(exc),
                metadata=metadata,
            )
            LOGGER.error("Ошибка отправки письма (%s): %s", _mask_email(normalized_email), exc)
            return "failed"

    def _prepare_route(self, to_email: str) -> RouteContext:
        domain = self._extract_domain(to_email)
        mx_result = self.mx_router.classify(domain) if domain else MXResult("UNKNOWN", [], False)
        provider = "yandex" if mx_result.classification == "RU" else "gmail"
        channel = self.yandex_settings if provider == "yandex" else self.gmail_settings
        reply_to: Optional[str] = None
        fallback = False

        if provider == "yandex" and not self._channel_configured(channel):
            LOGGER.warning(
                "Выбран Яндекс SMTP для %s, но конфигурация неполная. Фолбэк на Gmail.",
                domain or _mask_email(to_email),
            )
            provider = "gmail"
            channel = self.gmail_settings
            reply_to = None
            fallback = True

        if provider == "gmail" and not self._channel_configured(channel):
            channel = self.default_channel

        return RouteContext(
            provider=provider,
            channel=channel,
            mx_result=mx_result,
            reply_to=reply_to,
            fallback=fallback,
        )

    def _apply_headers(self, message: EmailMessage, channel: SMTPChannelSettings, *, reply_to: Optional[str]) -> None:
        if "From" in message:
            del message["From"]
        if "Reply-To" in message:
            del message["Reply-To"]
        message["From"] = self._build_from_header(channel)
        if reply_to:
            message["Reply-To"] = reply_to

    def _deliver_with_fallback(
        self,
        to_email: str,
        message: EmailMessage,
        route: RouteContext,
        metadata: Dict[str, object],
    ) -> RouteContext:
        try:
            self._send_via_channel(to_email, message, route.channel)
            return route
        except smtplib.SMTPAuthenticationError:
            raise
        except smtplib.SMTPException as exc:
            if self._should_fallback_to_gmail(route, exc):
                error_text = self._extract_smtp_error_text(exc)
                metadata["route"]["error"] = error_text
                fallback_route = self._build_gmail_route(route.mx_result)
                LOGGER.warning(
                    "Яндекс отклонил письмо %s как спам (%s). Переключаемся на Gmail.",
                    _mask_email(to_email),
                    error_text,
                )
                self._apply_headers(message, fallback_route.channel, reply_to=fallback_route.reply_to)
                try:
                    self._send_via_channel(to_email, message, fallback_route.channel)
                except smtplib.SMTPException as fallback_exc:
                    fallback_error = self._extract_smtp_error_text(fallback_exc)
                    metadata["route"]["error"] = f"{error_text} | fallback_failed: {fallback_error}"
                    raise
                else:
                    metadata["route"]["provider"] = fallback_route.provider
                    metadata["route"]["fallback"] = True
                    return fallback_route
            metadata["route"]["error"] = self._extract_smtp_error_text(exc)
            raise

    def _should_fallback_to_gmail(self, route: RouteContext, error: Exception) -> bool:
        if route.provider != "yandex" or route.fallback:
            return False
        if not self._channel_configured(self.gmail_settings):
            return False
        error_text = self._extract_smtp_error_text(error).lower()
        if not error_text:
            return False
        spam_tokens = (
            "5.7.1",
            "5.7.0",
            "suspected spam",
            "suspicion of spam",
            "message rejected",
            "under suspicion of spam",
        )
        return any(token in error_text for token in spam_tokens)

    @staticmethod
    def _extract_smtp_error_text(error: Exception) -> str:
        if isinstance(error, smtplib.SMTPResponseException):
            raw = error.smtp_error
            if isinstance(raw, bytes):
                return raw.decode("utf-8", "ignore")
            return str(raw)
        return str(error)

    def _build_gmail_route(self, mx_result: MXResult) -> RouteContext:
        return RouteContext(
            provider="gmail",
            channel=self.gmail_settings,
            mx_result=mx_result,
            reply_to=None,
            fallback=True,
        )

    @staticmethod
    def _channel_configured(channel: SMTPChannelSettings) -> bool:
        return bool(channel.host and channel.port)

    @staticmethod
    def _extract_domain(email: str) -> Optional[str]:
        _, parsed = parseaddr(email)
        if "@" not in parsed:
            return None
        return parsed.split("@", 1)[1].lower()

    def _send_via_channel(self, to_email: str, message: EmailMessage, channel: SMTPChannelSettings) -> None:
        if not channel.host:
            raise smtplib.SMTPException("SMTP host is not configured.")

        LOGGER.debug(
            "Отправка письма %s -> %s через %s",
            message["Message-ID"],
            _mask_email(to_email),
            channel.host,
        )
        if channel.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(channel.host, channel.port, timeout=self.timeout, context=context) as smtp:
                self._login_if_needed(smtp, channel)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(channel.host, channel.port, timeout=self.timeout) as smtp:
                if channel.use_tls and self.use_starttls:
                    smtp.starttls()
                self._login_if_needed(smtp, channel)
                smtp.send_message(message)

    @staticmethod
    def _login_if_needed(smtp: smtplib.SMTP, channel: SMTPChannelSettings) -> None:
        if channel.username and channel.password:
            smtp.login(channel.username, channel.password)

    def _make_message_id(self, channel: SMTPChannelSettings) -> str:
        domain = channel.host.split(":")[0] if channel.host else None
        return make_msgid(domain=domain)

    def _is_opt_out(self, session: Session, to_email: str) -> bool:
        normalized = clean_email(to_email)
        result = session.execute(text(CHECK_OPT_OUT_SQL), {"contact_value": normalized})
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

        delay_seconds = random.randint(MIN_SEND_DELAY_SECONDS, MAX_SEND_DELAY_SECONDS)
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
            candidate = base + timedelta(seconds=random.randint(MIN_SEND_DELAY_SECONDS, MAX_SEND_DELAY_SECONDS))

        return candidate

    def _is_within_send_window(self, local_dt: datetime) -> bool:
        start = datetime.combine(local_dt.date(), SEND_WINDOW_START, tzinfo=self._tz)
        end = datetime.combine(local_dt.date(), SEND_WINDOW_END, tzinfo=self._tz)
        return start <= local_dt <= end

    def is_within_send_window(self, *, reference: Optional[datetime] = None) -> bool:
        base = reference or datetime.now(timezone.utc)
        local_dt = base.astimezone(self._tz)
        return self._is_within_send_window(local_dt)
