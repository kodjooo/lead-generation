"""Обогащение компаний контактными данными с сайтов."""

from __future__ import annotations

import json
import logging
import re
import time
from hashlib import sha256
from dataclasses import dataclass
from unicodedata import category
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.modules.constants import HOMEPAGE_EXCERPT_LIMIT
from app.config import get_settings
from app.modules.utils.db import get_session_factory, session_scope
from app.modules.utils.email import clean_email, is_valid_email
from app.modules.utils.normalize import normalize_url

LOGGER = logging.getLogger("app.enrich_contacts")
EMAIL_TEXT_REGEX = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)


@dataclass
class ContactRecord:
    """Структурированная запись контакта."""

    contact_type: str
    value: str
    source_url: str
    quality_score: float
    origin: str = "text"
    label: Optional[str] = None

    def normalized_key(self) -> str:
        if self.contact_type == "email":
            return f"email:{clean_email(self.value)}"
        return f"other:{self.value}"


INSERT_CONTACT_SQL = """
INSERT INTO contacts (company_id, contact_type, value, source_url, is_primary, quality_score, metadata)
VALUES (:company_id, :contact_type, :value, :source_url, :is_primary, :quality_score, CAST(:metadata AS JSONB))
ON CONFLICT (contact_type, value)
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
        settings = get_settings()
        enrichment = settings.enrichment
        self.session_factory = session_factory or get_session_factory()
        self.timeout = enrichment.timeout_seconds if timeout == 10.0 else timeout
        self.max_redirects = enrichment.max_redirects
        self.headers = {
            "User-Agent": "LeadGenBot/1.0 (+https://example.com/bot-info)",
        }
        client_kwargs = {
            "timeout": self.timeout,
            "headers": self.headers,
            "follow_redirects": True,
            "max_redirects": self.max_redirects,
        }
        self.client = httpx.Client(**client_kwargs)
        self.proxy_urls = enrichment.proxy_urls
        self._proxy_health: Dict[str, float] = {}

    def enrich_company(
        self,
        company_id: str,
        canonical_domain: str,
        session: Optional[Session] = None,
    ) -> List[str]:
        """Запускает процесс обогащения и возвращает список идентификаторов контактов."""
        domain = (canonical_domain or "").strip()
        if not domain:
            LOGGER.warning("У компании %s отсутствует canonical_domain для обогащения.", company_id)
            return []

        if session is not None:
            return self._enrich_with_session(session, company_id, domain)

        with session_scope(self.session_factory) as scoped_session:
            return self._enrich_with_session(scoped_session, company_id, domain)

    def _enrich_with_session(
        self,
        session: Session,
        company_id: str,
        canonical_domain: str,
    ) -> List[str]:
        base_url = normalize_url(f"https://{canonical_domain}")
        if not base_url:
            LOGGER.warning("Не удалось нормализовать базовый URL для компании %s (%s).", company_id, canonical_domain)
            return []

        candidates = self._build_candidate_urls(base_url)
        collected_email: Optional[ContactRecord] = None
        homepage_excerpt_saved = False

        for candidate_url in candidates:
            html = self._fetch_html(candidate_url)
            if not html:
                continue
            if not homepage_excerpt_saved and self._is_homepage(candidate_url, base_url):
                self._save_homepage_excerpt(session, company_id, html)
                homepage_excerpt_saved = True
            if collected_email is None:
                for contact in self._extract_contacts_from_html(html, candidate_url):
                    if contact.contact_type == "email":
                        collected_email = contact
                        break
            if collected_email:
                break  # найден первый email, выходим

        if not homepage_excerpt_saved:
            html = self._fetch_html(base_url)
            if html:
                self._save_homepage_excerpt(session, company_id, html)

        if not collected_email:
            self._mark_company_status(session, company_id, "contacts_not_found")
            LOGGER.info("Контакты для компании %s не найдены.", company_id)
            return []

        inserted_ids: List[str] = []
        record = collected_email
        cleaned_value = clean_email(record.value)
        if cleaned_value and is_valid_email(cleaned_value):
            metadata = json.dumps({"label": record.label, "source_type": record.contact_type})
            result = session.execute(
                text(INSERT_CONTACT_SQL),
                {
                    "company_id": company_id,
                    "contact_type": record.contact_type,
                    "value": cleaned_value,
                    "source_url": record.source_url,
                    "is_primary": True,
                    "quality_score": record.quality_score,
                    "metadata": metadata,
                },
            )
            inserted_ids.append(str(result.scalar_one()))
        else:
            LOGGER.debug(
                "Получен невалидный e-mail '%s' для компании %s — пропускаем запись.",
                record.value,
                company_id,
            )

        if inserted_ids:
            self._mark_company_status(session, company_id, "contacts_ready")

        return inserted_ids

    def _build_candidate_urls(self, base_url: str) -> List[str]:
        suffixes = [
            "/",
            "/contact",
            "/contacts",
            "/contact-us",
            "/contacts/",
            "/about",
            "/about-us",
            "/kontakty",
            "/rekvizity",
            "/company",
        ]
        seen: Set[str] = set()
        candidates: List[str] = []
        for suffix in suffixes:
            candidate = urljoin(base_url, suffix)
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
        return candidates

    def _fetch_html(self, url: str) -> str:
        clients = self._clients_for_url(url)
        for client, proxy_url in clients:
            if proxy_url and not self._proxy_available(proxy_url):
                continue
            try:
                response = client.get(url)
                if response.status_code >= 500:
                    LOGGER.debug("Страница %s вернула статус %s через %s", url, response.status_code, proxy_url or "direct")
                    self._mark_proxy_failed(proxy_url)
                    continue
                if response.status_code >= 400:
                    LOGGER.debug("Страница %s вернула статус %s", url, response.status_code)
                    return ""
                if proxy_url:
                    self._mark_proxy_success(proxy_url)
                return response.text
            except httpx.HTTPError as exc:  # noqa: PERF203
                LOGGER.debug("Не удалось загрузить %s через %s: %s", url, proxy_url or "direct", exc)
                self._mark_proxy_failed(proxy_url)
                continue
        return ""

    def _clients_for_url(self, url: str) -> List[tuple[httpx.Client, Optional[str]]]:
        if not self.proxy_urls:
            return [(self.client, None)]
        ordered = list(self.proxy_urls)
        index = int(sha256(url.encode("utf-8")).hexdigest(), 16) % len(ordered)
        ordered = ordered[index:] + ordered[:index]
        clients: List[tuple[httpx.Client, Optional[str]]] = [(self.client, None)]
        for proxy_url in ordered:
            clients.append((self._proxy_client(proxy_url), proxy_url))
        return clients

    def _proxy_client(self, proxy_url: str) -> httpx.Client:
        if getattr(self, "_proxy_cache", None) is None:
            self._proxy_cache = {}
        proxy_cache = self._proxy_cache  # type: ignore[attr-defined]
        if proxy_url not in proxy_cache:
            proxy_cache[proxy_url] = httpx.Client(
                timeout=self.timeout,
                headers=self.headers,
                follow_redirects=True,
                max_redirects=self.max_redirects,
                proxy=proxy_url,
            )
        return proxy_cache[proxy_url]

    def _proxy_available(self, proxy_url: str) -> bool:
        retry_after = self._proxy_health.get(proxy_url)
        if retry_after is None:
            return True
        return retry_after <= time.time()

    def _mark_proxy_failed(self, proxy_url: Optional[str]) -> None:
        if not proxy_url:
            return
        self._proxy_health[proxy_url] = time.time() + 300

    def _mark_proxy_success(self, proxy_url: Optional[str]) -> None:
        if not proxy_url:
            return
        self._proxy_health.pop(proxy_url, None)

    def _extract_contacts_from_html(self, html: str, source_url: str) -> Iterable[ContactRecord]:
        soup = BeautifulSoup(html, "html.parser")
        found_email: Optional[ContactRecord] = None
        seen: Set[str] = set()
        records: List[ContactRecord] = []

        # mailto/tel ссылки
        for anchor in soup.find_all("a"):
            href = (anchor.get("href") or "").strip()
            text = anchor.get_text(" ", strip=True)
            if href.lower().startswith("mailto:"):
                email = href.split(":", 1)[1]
                cleaned = clean_email(email)
                if not is_valid_email(cleaned):
                    LOGGER.debug("Пропускаем mailto без валидного e-mail: %s", email)
                    continue
                key = f"email:{cleaned}"
                if key in seen:
                    continue
                seen.add(key)
                records.append(
                    ContactRecord("email", cleaned, source_url, 1.0, origin="mailto", label=text or "mailto")
                )

            for attr_name in ("data-email", "data-mail", "href"):
                attr_value = (anchor.get(attr_name) or "").strip()
                for email in self._find_emails(attr_value):
                    key = f"email:{email}"
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(
                        ContactRecord("email", email, source_url, 0.92, origin="attribute", label=text or attr_name)
                    )

        for email in self._find_emails(soup.get_text(" ", strip=True)):
            key = f"email:{email}"
            if key in seen:
                continue
            seen.add(key)
            records.append(ContactRecord("email", email, source_url, 0.85, origin="text", label="text"))

        if records:
            records.sort(key=lambda record: record.quality_score, reverse=True)
            found_email = records[0]

        if found_email:
            return [found_email]

        return []

    @staticmethod
    def _find_emails(value: str) -> List[str]:
        if not value:
            return []
        emails: List[str] = []
        seen: Set[str] = set()
        for match in EMAIL_TEXT_REGEX.findall(value):
            cleaned = clean_email(match)
            if not is_valid_email(cleaned) or cleaned in seen:
                continue
            seen.add(cleaned)
            emails.append(cleaned)
        return emails

    @staticmethod
    def _mark_company_status(session: Session, company_id: str, status: str) -> None:
        session.execute(
            text("UPDATE companies SET status = :status, updated_at = NOW() WHERE id = :id"),
            {"status": status, "id": company_id},
        )

    @staticmethod
    def _is_homepage(candidate_url: str, base_url: str) -> bool:
        return candidate_url.rstrip("/") == base_url.rstrip("/")

    def _save_homepage_excerpt(self, session: Session, company_id: str, html: str) -> None:
        soup = BeautifulSoup(html, "html.parser")
        text_content = soup.get_text(" ", strip=True)
        if not text_content:
            return
        excerpt = self._sanitize_excerpt(text_content)[:HOMEPAGE_EXCERPT_LIMIT]
        if not excerpt:
            return
        patch = json.dumps({"homepage_excerpt": excerpt})
        session.execute(
            text(
                "UPDATE companies SET attributes = attributes || CAST(:patch AS JSONB) WHERE id = :company_id"
            ),
            {"company_id": company_id, "patch": patch},
        )

    @staticmethod
    def _sanitize_excerpt(text_value: str) -> str:
        """Удаляет невалидные для PostgreSQL JSON символы (например, NUL)."""
        if not text_value:
            return ""
        return "".join(ch for ch in text_value if ch != "\x00" and category(ch) != "Cc")
