"""Обогащение компаний контактными данными с сайтов."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.modules.utils.db import get_session_factory, session_scope
from app.modules.utils.normalize import normalize_url

LOGGER = logging.getLogger("app.enrich_contacts")

EMAIL_REGEX = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_REGEX = re.compile(r"\+?\d[\d\s().-]{7,}")


@dataclass
class ContactRecord:
    """Структурированная запись контакта."""

    contact_type: str
    value: str
    source_url: str
    quality_score: float
    label: Optional[str] = None

    def normalized_key(self) -> str:
        if self.contact_type == "email":
            return f"{self.contact_type}:{self.value.lower()}"
        cleaned = re.sub(r"[^\d+]", "", self.value)
        return f"{self.contact_type}:{cleaned}"


INSERT_CONTACT_SQL = """
INSERT INTO contacts (company_id, contact_type, value, source_url, is_primary, quality_score, metadata)
VALUES (:company_id, :contact_type, :value, :source_url, :is_primary, :quality_score, CAST(:metadata AS JSONB))
ON CONFLICT ON CONSTRAINT uidx_contacts_value_type
DO UPDATE SET
    company_id = EXCLUDED.company_id,
    source_url = COALESCE(EXCLUDED.source_url, contacts.source_url),
    quality_score = GREATEST(contacts.quality_score, EXCLUDED.quality_score),
    last_seen_at = NOW(),
    metadata = contacts.metadata || EXCLUDED.metadata
RETURNING id;
"""


class ContactEnricher:
    """Извлекает контакты из веб-страниц и сохраняет их в БД."""

    def __init__(
        self,
        *,
        session_factory: Optional[sessionmaker[Session]] = None,
        timeout: float = 10.0,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()
        self.timeout = timeout
        self.headers = {
            "User-Agent": "LeadGenBot/1.0 (+https://example.com/bot-info)",
        }

    def enrich_company(
        self,
        company_id: str,
        website_url: str,
        session: Optional[Session] = None,
    ) -> List[str]:
        """Запускает процесс обогащения и возвращает список идентификаторов контактов."""
        if not website_url:
            LOGGER.warning("У компании %s отсутствует URL для обогащения.", company_id)
            return []

        if session is not None:
            return self._enrich_with_session(session, company_id, website_url)

        with session_scope(self.session_factory) as scoped_session:
            return self._enrich_with_session(scoped_session, company_id, website_url)

    def _enrich_with_session(
        self,
        session: Session,
        company_id: str,
        website_url: str,
    ) -> List[str]:
        base_url = normalize_url(website_url)
        candidates = self._build_candidate_urls(base_url)
        collected: Dict[str, ContactRecord] = {}

        for candidate_url in candidates:
            html = self._fetch_html(candidate_url)
            if not html:
                continue
            for contact in self._extract_contacts_from_html(html, candidate_url):
                collected[contact.normalized_key()] = contact
            if collected:
                break  # Контакты найдены, можно остановиться.

        if not collected:
            LOGGER.info("Контакты для компании %s не найдены.", company_id)
            return []

        inserted_ids: List[str] = []
        primary_assigned: Set[str] = set()
        for record in collected.values():
            is_primary = False
            if record.contact_type == "email" and "email" not in primary_assigned:
                is_primary = True
                primary_assigned.add("email")
            elif record.contact_type == "phone" and "phone" not in primary_assigned:
                is_primary = True
                primary_assigned.add("phone")

            cleaned_value = re.sub(r"\s+", " ", record.value).replace("\u00a0", " ").strip()
            metadata = json.dumps({"label": record.label, "source_type": record.contact_type})
            result = session.execute(
                text(INSERT_CONTACT_SQL),
                {
                    "company_id": company_id,
                    "contact_type": record.contact_type,
                    "value": cleaned_value,
                    "source_url": record.source_url,
                    "is_primary": is_primary,
                    "quality_score": record.quality_score,
                    "metadata": metadata,
                },
            )
            inserted_ids.append(str(result.scalar_one()))

        return inserted_ids

    def _build_candidate_urls(self, base_url: str) -> List[str]:
        suffixes = ["/", "/contact", "/contacts", "/about", "/about-us", "/kontakty"]
        seen: Set[str] = set()
        candidates: List[str] = []
        for suffix in suffixes:
            candidate = urljoin(base_url, suffix)
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
        return candidates

    def _fetch_html(self, url: str) -> str:
        try:
            response = httpx.get(url, timeout=self.timeout, headers=self.headers, follow_redirects=True)
            if response.status_code >= 400:
                LOGGER.debug("Страница %s вернула статус %s", url, response.status_code)
                return ""
            return response.text
        except httpx.HTTPError as exc:  # noqa: PERF203
            LOGGER.debug("Не удалось загрузить %s: %s", url, exc)
            return ""

    def _extract_contacts_from_html(self, html: str, source_url: str) -> Iterable[ContactRecord]:
        soup = BeautifulSoup(html, "html.parser")
        found: Dict[str, ContactRecord] = {}

        # mailto/tel ссылки
        for anchor in soup.find_all("a"):
            href = (anchor.get("href") or "").strip()
            text = anchor.get_text(" ", strip=True)
            if href.lower().startswith("mailto:"):
                email = href.split(":", 1)[1].split("?", 1)[0]
                record = ContactRecord("email", email, source_url, 1.0, label=text or "mailto")
                found[record.normalized_key()] = record
            elif href.lower().startswith("tel:"):
                phone = href.split(":", 1)[1].split("?", 1)[0]
                record = ContactRecord("phone", phone, source_url, 0.9, label=text or "tel")
                found[record.normalized_key()] = record

        text_blob = soup.get_text(" ", strip=True)
        for match in EMAIL_REGEX.finditer(text_blob):
            email = match.group(0)
            record = ContactRecord("email", email, source_url, 0.8, label="text")
            found.setdefault(record.normalized_key(), record)

        for match in PHONE_REGEX.finditer(text_blob):
            phone = match.group(0)
            record = ContactRecord("phone", phone, source_url, 0.6, label="text")
            found.setdefault(record.normalized_key(), record)

        return list(found.values())
