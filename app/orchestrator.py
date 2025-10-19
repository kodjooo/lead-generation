"""Оркестратор пайплайна лидогенерации."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.modules.deduplicate import DeduplicationService
from app.modules.enrich_contacts import ContactEnricher
from app.modules.generate_email_gpt import CompanyBrief, EmailGenerator, OfferBrief
from app.modules.send_email import EmailSender
from app.modules.serp_ingest import SerpIngestService
from app.modules.utils.db import get_session_factory, session_scope
from app.modules.utils.iam import (
    IamTokenProvider,
    StaticTokenProvider,
    load_service_account_key_from_file,
    load_service_account_key_from_string,
)
from app.modules.yandex_deferred import DeferredQueryParams, OperationResponse, YandexDeferredClient
from app.modules.sheet_sync import build_service as build_sheet_sync_service

LOGGER = logging.getLogger("app.orchestrator")

SELECT_PENDING_QUERIES_SQL = """
SELECT id, query_text, region_code
FROM serp_queries
WHERE status = 'pending'
  AND scheduled_for <= NOW()
ORDER BY scheduled_for ASC
LIMIT :limit;
"""

INSERT_OPERATION_SQL = """
INSERT INTO serp_operations (
    query_id,
    operation_id,
    status,
    requested_at,
    metadata
)
VALUES (
    :query_id,
    :operation_id,
    'created',
    NOW(),
    CAST(:metadata AS JSONB)
)
ON CONFLICT (operation_id) DO NOTHING;
"""

UPDATE_QUERY_STATUS_SQL = """
UPDATE serp_queries
SET status = :status,
    updated_at = NOW()
WHERE id = :query_id;
"""

SELECT_OPEN_OPERATIONS_SQL = """
SELECT id, query_id, operation_id, status
FROM serp_operations
WHERE status IN ('created', 'running')
ORDER BY requested_at
LIMIT :limit;
"""

UPDATE_OPERATION_STATUS_SQL = """
UPDATE serp_operations
SET status = :status,
    completed_at = :completed_at,
    retry_count = retry_count + :increment_retry,
    error_payload = CAST(:error_payload AS JSONB),
    metadata = metadata || CAST(:metadata AS JSONB),
    modified_at = NOW()
WHERE id = :operation_id;
"""

SELECT_COMPANIES_WITHOUT_CONTACTS_SQL = """
SELECT c.id, COALESCE(c.website_url, 'https://' || c.canonical_domain) AS website_url
FROM companies c
LEFT JOIN contacts ct ON ct.company_id = c.id
WHERE ct.id IS NULL
  AND COALESCE(c.website_url, c.canonical_domain) IS NOT NULL
ORDER BY c.created_at
LIMIT :limit;
"""

SELECT_CONTACTS_FOR_OUTREACH_SQL = """
SELECT ct.id AS contact_id, ct.company_id, ct.value, c.name, c.canonical_domain, c.industry
FROM contacts ct
JOIN companies c ON c.id = ct.company_id
LEFT JOIN outreach_messages om ON om.contact_id = ct.id AND om.status IN ('sent', 'scheduled')
LEFT JOIN opt_out_registry o ON LOWER(o.contact_value) = LOWER(ct.value)
WHERE ct.contact_type = 'email'
  AND om.id IS NULL
  AND o.id IS NULL
ORDER BY ct.first_seen_at
LIMIT :limit;
"""

SELECT_SERP_QUERY_DETAILS_SQL = """
SELECT query_text, region_code
FROM serp_queries
WHERE id = :query_id;
"""


@dataclass
class OrchestratorConfig:
    batch_size: int = 5
    poll_interval_seconds: int = 60


class PipelineOrchestrator:
    """Связывает все модули в единую последовательность шагов."""

    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config or OrchestratorConfig()
        self.session_factory = get_session_factory()
        settings = get_settings()
        token_provider = self._build_iam_provider(settings)
        self.deferred_client = YandexDeferredClient(
            token_provider=token_provider.get_token,
            folder_id=settings.yandex_folder_id,
            timezone=settings.timezone,
            enforce_night_window=settings.yandex_enforce_night_window,
        )
        self.serp_ingest = SerpIngestService(self.session_factory)
        self.deduplicator = DeduplicationService(self.session_factory)
        self.contact_enricher = ContactEnricher(session_factory=self.session_factory)
        self.email_generator = EmailGenerator()
        self.email_sender = EmailSender(session_factory=self.session_factory)
        self.offer = OfferBrief(
            pains=["Расширение воронки B2B", "Высокая стоимость лида"],
            value_proposition="Автоматизируем поиск релевантных компаний и персонализируем писма в течение суток.",
            call_to_action="Готовы обсудить 15-минутный пилот на этой неделе?",
        )
        self._token_provider = token_provider
        self.sheet_settings = settings.sheet_sync
        self._sheet_service = None
        self._sheet_sync_interval = timedelta(minutes=max(1, self.sheet_settings.interval_minutes))
        self._last_sheet_sync: datetime | None = None
        if self.sheet_settings.enabled:
            try:
                self._sheet_service = build_sheet_sync_service(settings)
                LOGGER.info(
                    "Автосинхронизация Google Sheets включена (каждые %s мин, batch_tag=%s)",
                    self.sheet_settings.interval_minutes,
                    self.sheet_settings.batch_tag,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Не удалось инициализировать синхронизацию Google Sheets: %s", exc)
                self._sheet_service = None

    @staticmethod
    def _build_iam_provider(settings) -> StaticTokenProvider | IamTokenProvider:
        if settings.yandex_iam_token:
            return StaticTokenProvider(settings.yandex_iam_token)

        if settings.yandex_sa_key_path:
            key = load_service_account_key_from_file(Path(settings.yandex_sa_key_path))
            return IamTokenProvider(key=key)

        if settings.yandex_sa_key_json:
            key = load_service_account_key_from_string(settings.yandex_sa_key_json)
            return IamTokenProvider(key=key)

        raise RuntimeError(
            "Не настроена авторизация Yandex Cloud: задайте YANDEX_CLOUD_IAM_TOKEN "
            "или путь/JSON ключа сервисного аккаунта."
        )

    def run_once(self) -> None:
        self._maybe_sync_sheet()
        LOGGER.info("Выполнение цикла оркестрации.")
        scheduled = self._schedule_deferred_queries()
        processed = self._poll_operations()
        if processed:
            self.deduplicator.run()
        enriched = self._enrich_missing_contacts()
        sent = self._generate_and_send_emails()
        LOGGER.info(
            "Цикл завершён: scheduled=%s, processed=%s, enriched=%s, sent=%s",
            scheduled,
            processed,
            enriched,
            sent,
        )

    def run_forever(self) -> None:
        LOGGER.info("Запуск оркестратора в режиме цикла (%s c).", self.config.poll_interval_seconds)
        while True:
            self.run_once()
            time.sleep(self.config.poll_interval_seconds)

    def schedule_deferred_queries(self) -> int:
        """Публичный метод для планировщика."""
        return self._schedule_deferred_queries()

    def poll_operations(self) -> int:
        """Публичный метод для обновления статусов операций."""
        return self._poll_operations()

    def enrich_missing_contacts(self) -> int:
        """Публичный метод для воркера контактов."""
        return self._enrich_missing_contacts()

    def generate_and_send_emails(self) -> int:
        """Генерация и отправка писем."""
        return self._generate_and_send_emails()

    def _maybe_sync_sheet(self) -> None:
        if not self._sheet_service:
            return
        now = datetime.now(timezone.utc)
        if self._last_sheet_sync and now - self._last_sheet_sync < self._sheet_sync_interval:
            return
        try:
            summary = self._sheet_service.sync(batch_tag=self.sheet_settings.batch_tag)
            LOGGER.info(
                "Синхронизация Google Sheets: обработано=%s, добавлено=%s, дубликатов=%s, ошибок=%s",
                summary.processed_rows,
                summary.inserted_queries,
                summary.duplicate_queries,
                summary.errors,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Ошибка синхронизации Google Sheets: %s", exc)
        finally:
            self._last_sheet_sync = now

    def _schedule_deferred_queries(self) -> int:
        with session_scope(self.session_factory) as session:
            rows = list(
                session.execute(
                    text(SELECT_PENDING_QUERIES_SQL),
                    {"limit": self.config.batch_size},
                ).mappings()
            )
            if not rows:
                return 0

            scheduled = 0
            for row in rows:
                try:
                    params = DeferredQueryParams(query_text=row["query_text"], region=row["region_code"])
                    operation = self.deferred_client.create_deferred_search(params)
                    session.execute(
                        text(INSERT_OPERATION_SQL),
                        {
                            "query_id": row["id"],
                            "operation_id": operation.id,
                            "metadata": json.dumps({"created_at": datetime.now(timezone.utc).isoformat()}),
                        },
                    )
                    session.execute(
                        text(UPDATE_QUERY_STATUS_SQL),
                        {"query_id": row["id"], "status": "in_progress"},
                    )
                    scheduled += 1
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("Не удалось создать deferred-запрос: %s", exc)
            return scheduled

    def _poll_operations(self) -> int:
        with session_scope(self.session_factory) as session:
            rows = list(
                session.execute(
                    text(SELECT_OPEN_OPERATIONS_SQL),
                    {"limit": self.config.batch_size},
                ).mappings()
            )
            if not rows:
                return 0

            processed = 0
            for row in rows:
                operation_id = row["operation_id"]
                try:
                    operation = self.deferred_client.get_operation(operation_id)
                    status = "running" if not operation.done else "done"
                    metadata = {"last_checked": datetime.now(timezone.utc).isoformat()}

                    if operation.done:
                        self._handle_completed_operation(session, row["query_id"], operation)
                        processed += 1
                        status = "done"
                        completed_at = datetime.now(timezone.utc)
                    else:
                        completed_at = None

                    session.execute(
                        text(UPDATE_OPERATION_STATUS_SQL),
                        {
                            "operation_id": row["id"],
                            "status": status,
                            "completed_at": completed_at,
                            "increment_retry": 0,
                            "error_payload": None,
                            "metadata": json.dumps(metadata),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("Ошибка обработки операции %s: %s", operation_id, exc)
                    session.execute(
                        text(UPDATE_OPERATION_STATUS_SQL),
                        {
                            "operation_id": row["id"],
                            "status": "failed",
                            "completed_at": datetime.now(timezone.utc),
                            "increment_retry": 1,
                            "error_payload": json.dumps({"reason": str(exc)}),
                            "metadata": json.dumps({}),
                        },
                    )
            return processed

    def _handle_completed_operation(self, session: Session, query_id: str, operation: OperationResponse) -> None:
        try:
            raw_xml = operation.decode_raw_data()
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Не удалось декодировать ответ операции %s: %s", operation.id, exc)
            return

        self.serp_ingest.ingest(operation.id, raw_xml)
        session.execute(
            text(UPDATE_QUERY_STATUS_SQL),
            {"query_id": query_id, "status": "completed"},
        )

    def _enrich_missing_contacts(self) -> int:
        with session_scope(self.session_factory) as session:
            rows = list(
                session.execute(
                    text(SELECT_COMPANIES_WITHOUT_CONTACTS_SQL),
                    {"limit": self.config.batch_size},
                ).mappings()
            )
            count = 0
            for row in rows:
                inserted = self.contact_enricher.enrich_company(
                    company_id=str(row["id"]),
                    website_url=row["website_url"],
                    session=session,
                )
                if inserted:
                    count += 1
            return count

    def _generate_and_send_emails(self) -> int:
        with session_scope(self.session_factory) as session:
            rows = list(
                session.execute(
                    text(SELECT_CONTACTS_FOR_OUTREACH_SQL),
                    {"limit": self.config.batch_size},
                ).mappings()
            )
            sent = 0
            for row in rows:
                company = CompanyBrief(
                    name=row["name"],
                    domain=row["canonical_domain"] or row["value"].split("@")[-1],
                    industry=row["industry"],
                )
                template = self.email_generator.generate(company, self.offer)
                self.email_sender.send(
                    company_id=row["company_id"],
                    contact_id=row["contact_id"],
                    to_email=row["value"],
                    template=template,
                    session=session,
                )
                sent += 1
            return sent
