"""Обработка XML-ответов Yandex Search и сохранение результатов."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.modules.utils.db import get_session_factory, session_scope
from app.modules.utils.normalize import (
    build_company_dedupe_key,
    clean_snippet,
    normalize_domain,
    normalize_url,
)

LOGGER = logging.getLogger("app.serp_ingest")


class SerpParseError(RuntimeError):
    """Ошибка парсинга XML-ответа."""


@dataclass
class SerpDocument:
    """Нормализованный документ выдачи."""

    url: str
    domain: str
    title: str
    snippet: str
    position: int
    language: Optional[str]


def parse_serp_xml(xml_payload: bytes) -> List[SerpDocument]:
    """Извлекает документы из XML-ответа Yandex Search."""
    if not xml_payload:
        return []

    try:
        root = ET.fromstring(xml_payload)
    except ET.ParseError as exc:
        raise SerpParseError("Некорректный XML выдачи.") from exc

    documents: List[SerpDocument] = []
    for position, doc in enumerate(root.findall(".//doc"), start=1):
        url_text = (doc.findtext("url") or doc.findtext("lurl") or "").strip()
        normalized_url = normalize_url(url_text)
        if not normalized_url:
            LOGGER.debug("Пропущен документ без корректного URL: %s", url_text)
            continue

        domain_text = doc.findtext("domain") or ""
        normalized_domain = normalize_domain(domain_text or normalized_url)
        title = (doc.findtext("title") or doc.findtext("name") or normalized_domain).strip()

        passages = [clean_snippet(node.text) for node in doc.findall(".//passages/passage")]
        snippet = clean_snippet(" ".join(filter(None, passages)))

        language = None
        for prop in doc.findall(".//properties/property"):
            if prop.get("name") == "lang" and prop.text:
                language = prop.text.strip()
                break

        documents.append(
            SerpDocument(
                url=normalized_url,
                domain=normalized_domain,
                title=title,
                snippet=snippet,
                position=position,
                language=language,
            )
        )

    return documents


INSERT_SERP_RESULT_SQL = """
INSERT INTO serp_results (operation_id, url, domain, title, snippet, position, language, metadata)
VALUES (:operation_id, :url, :domain, :title, :snippet, :position, :language, :metadata::jsonb)
ON CONFLICT (operation_id, url)
DO UPDATE SET
    title = EXCLUDED.title,
    snippet = EXCLUDED.snippet,
    position = EXCLUDED.position,
    language = EXCLUDED.language,
    metadata = serp_results.metadata || EXCLUDED.metadata
RETURNING id;
"""


UPSERT_COMPANY_SQL = """
INSERT INTO companies (
    name,
    canonical_domain,
    website_url,
    status,
    dedupe_hash,
    attributes,
    source,
    first_seen_at,
    last_seen_at
)
VALUES (
    :name,
    :domain,
    :website_url,
    'new',
    :dedupe_hash,
    :attributes::jsonb,
    'yandex_search_api',
    NOW(),
    NOW()
)
ON CONFLICT (dedupe_hash)
DO UPDATE SET
    website_url = COALESCE(companies.website_url, EXCLUDED.website_url),
    attributes = companies.attributes || EXCLUDED.attributes,
    last_seen_at = NOW(),
    updated_at = NOW()
RETURNING id;
"""


class SerpIngestService:
    """Сохраняет документы выдачи в БД."""

    def __init__(self, session_factory: Optional[sessionmaker[Session]] = None) -> None:
        self.session_factory = session_factory or get_session_factory()

    def ingest(self, operation_id: str, xml_payload: bytes) -> List[str]:
        """Парсит и сохраняет результаты выдачи для операции."""
        documents = parse_serp_xml(xml_payload)
        if not documents:
            LOGGER.info("Операция %s не содержит документов для сохранения.", operation_id)
            return []

        inserted: List[str] = []
        with session_scope(self.session_factory) as session:
            for document in documents:
                result_id = self._upsert_result(session, operation_id, document)
                inserted.append(result_id)
                self._ensure_company(session, document)

        return inserted

    def _upsert_result(self, session: Session, operation_id: str, document: SerpDocument) -> str:
        metadata = json.dumps({"language": document.language, "source": "yandex"})
        result = session.execute(
            text(INSERT_SERP_RESULT_SQL),
            {
                "operation_id": operation_id,
                "url": document.url,
                "domain": document.domain,
                "title": document.title,
                "snippet": document.snippet,
                "position": document.position,
                "language": document.language,
                "metadata": metadata,
            },
        )
        return str(result.scalar_one())

    def _ensure_company(self, session: Session, document: SerpDocument) -> None:
        dedupe_hash = build_company_dedupe_key(document.title, document.domain)
        attributes = json.dumps({
            "source": "yandex_serp",
            "last_snippet": document.snippet,
        })
        session.execute(
            text(UPSERT_COMPANY_SQL),
            {
                "name": document.title or document.domain,
                "domain": document.domain or None,
                "website_url": document.url,
                "dedupe_hash": dedupe_hash,
                "attributes": attributes,
            },
        )
