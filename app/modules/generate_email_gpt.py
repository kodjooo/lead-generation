"""Генерация персонализированных писем с помощью LLM."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from app.config import get_settings

LOGGER = logging.getLogger("app.generate_email")
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"


@dataclass
class CompanyBrief:
    """Минимальное описание компании для письма."""

    name: str
    domain: str
    industry: Optional[str] = None
    highlights: List[str] = field(default_factory=list)


@dataclass
class ContactBrief:
    """Информация о контактном лице."""

    name: Optional[str] = None
    role: Optional[str] = None


@dataclass
class OfferBrief:
    """Предложение и ключевые боли клиента."""

    pains: List[str] = field(default_factory=list)
    value_proposition: str = ""
    call_to_action: str = "Давайте обсудим возможности сотрудничества на коротком созвоне."  # noqa: E501


@dataclass
class EmailTemplate:
    """Готовое письмо."""

    subject: str
    body: str


class EmailGenerator:
    """Инкапсулирует обращение к LLM и fallback-шаблон."""

    def __init__(
        self,
        *,
        model: str = "gpt-4.1-mini",
        language: str = "ru",
        temperature: float = 0.4,
        timeout: float = 15.0,
    ) -> None:
        self.model = model
        self.language = language
        self.temperature = temperature
        self.timeout = timeout
        self.settings = get_settings()

    def generate(
        self,
        company: CompanyBrief,
        offer: OfferBrief,
        contact: Optional[ContactBrief] = None,
    ) -> EmailTemplate:
        """Возвращает готовый шаблон письма."""
        if not self.settings.openai_api_key:
            LOGGER.warning("OPENAI_API_KEY не задан, используется fallback-шаблон.")
            return self._fallback_template(company, offer, contact)

        try:
            response = self._request_openai(company, offer, contact)
            return self._parse_openai_response(response) or self._fallback_template(company, offer, contact)
        except httpx.HTTPError as exc:  # noqa: PERF203
            LOGGER.error("Ошибка обращения к OpenAI: %s", exc)
            return self._fallback_template(company, offer, contact)

    def _request_openai(
        self,
        company: CompanyBrief,
        offer: OfferBrief,
        contact: Optional[ContactBrief],
    ) -> Dict[str, object]:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_schema", "json_schema": self._response_schema()},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты ассистент по продажам. Пиши короткие B2B письма на русском языке. "
                        "Формат ответа — JSON c полями subject и body."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "company": {
                                "name": company.name,
                                "domain": company.domain,
                                "industry": company.industry,
                                "highlights": company.highlights,
                            },
                            "contact": {
                                "name": contact.name if contact else None,
                                "role": contact.role if contact else None,
                            },
                            "offer": {
                                "pains": offer.pains,
                                "value_proposition": offer.value_proposition,
                                "call_to_action": offer.call_to_action,
                            },
                            "language": self.language,
                        }
                    ),
                },
            ],
        }

        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }

        LOGGER.debug("Запрос к OpenAI: %s", payload)
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(OPENAI_CHAT_COMPLETIONS_URL, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    def _parse_openai_response(self, response: Dict[str, object]) -> Optional[EmailTemplate]:
        try:
            choices = response.get("choices", [])
            if not choices:
                return None
            message = choices[0]["message"]  # type: ignore[index]
            content = message.get("content")
            if not content:
                return None
            parsed = json.loads(content)
            return EmailTemplate(subject=parsed["subject"], body=parsed["body"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            LOGGER.error("Не удалось интерпретировать ответ LLM: %s", response)
            return None

    def _fallback_template(
        self,
        company: CompanyBrief,
        offer: OfferBrief,
        contact: Optional[ContactBrief],
    ) -> EmailTemplate:
        contact_name = contact.name if contact and contact.name else "Коллеги"
        pains = "\n- " + "\n- ".join(offer.pains) if offer.pains else ""
        subject = f"Идеи по росту {company.name}" if company.name else "Предложение по сотрудничеству"
        body = (
            f"{contact_name}, здравствуйте!\n\n"
            f"Меня зовут команда LeadGen. Мы изучили сайт {company.domain}"
            f" и подготовили небольшие предложения по улучшению процессов."
        )
        if pains:
            body += f"\n\nМы обратили внимание на задачи:{pains}"
        if offer.value_proposition:
            body += f"\n\nЧто мы предлагаем: {offer.value_proposition}"
        body += f"\n\n{offer.call_to_action}\n\nС уважением, команда LeadGen"
        return EmailTemplate(subject=subject, body=body)

    def _response_schema(self) -> Dict[str, object]:
        return {
            "name": "EmailTemplate",
            "schema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["subject", "body"],
            },
        }
