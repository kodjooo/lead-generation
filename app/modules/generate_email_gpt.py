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
    emails: List[str] = field(default_factory=list)
    phones: List[str] = field(default_factory=list)


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


@dataclass
class GeneratedEmail:
    """Результат генерации письма вместе с исходным запросом."""

    template: EmailTemplate
    request_payload: Optional[Dict[str, object]] = None
    used_fallback: bool = False


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
    ) -> GeneratedEmail:
        """Возвращает готовый шаблон и исходный запрос к LLM."""
        payload: Optional[Dict[str, object]] = None
        if not self.settings.openai_api_key:
            LOGGER.warning("OPENAI_API_KEY не задан, используется fallback-шаблон.")
            template = self._fallback_template(company, offer, contact)
            return GeneratedEmail(template=template, request_payload=None, used_fallback=True)

        try:
            payload = self._build_payload(company, offer, contact)
            response = self._request_openai(payload)
            parsed = self._parse_openai_response(response)
            if parsed:
                return GeneratedEmail(template=parsed, request_payload=payload, used_fallback=False)
            template = self._fallback_template(company, offer, contact)
            return GeneratedEmail(template=template, request_payload=payload, used_fallback=True)
        except httpx.HTTPError as exc:  # noqa: PERF203
            LOGGER.error("Ошибка обращения к OpenAI: %s", exc)
            template = self._fallback_template(company, offer, contact)
            return GeneratedEmail(template=template, request_payload=payload, used_fallback=True)

    def _build_payload(
        self,
        company: CompanyBrief,
        offer: OfferBrief,
        contact: Optional[ContactBrief],
    ) -> Dict[str, object]:
        homepage_excerpt = " ".join(company.highlights) if company.highlights else None
        return {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_schema", "json_schema": self._response_schema()},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Ты Марк Аборчи, специалист по AI-автоматизации. Твоя задача — писать "
                        "персонализированные, человеческие письма на русском языке для компаний, "
                        "которым можно помочь автоматизацией процессов с помощью нейросетей, Python, make.com или n8n. "
                        "Избегай рекламного тона и превосходных степеней. Делай акцент на пользе: экономия времени, "
                        "сокращение затрат, устранение рутины, повышение эффективности. Всегда используй JSON-ответ с полями subject и body. "
                        "Структура письма фиксирована: тема передаёт идею оптимизации процессов компании (например, 'Идея по оптимизации процессов вашей компании') и тело состоит из блоков:\n"
                        "1) Приветствие 'Добрый день!'.\n"
                        "2) Короткое представление Марка и его подхода (нейросети, Python).\n"
                        "3) Упоминание, чем занимается компания (используй предоставленный текст, не упоминай название). Добавь короткое наблюдение (1 предложение) о чём-то, что выделяет компанию: что тебя впечатлило, что показалось интересным.\n"
                        "4) Описание конкретного процесса, который можно упростить с помощью AI, и ожидаемого эффекта (сократить задержки, уменьшить затраты и т.п.).\n"
                        "5) Приглашение обсудить примеры.\n"
                        "6) Завершение: 'С уважением,' + имя и должность.\n"
                        "Структуру сохраняй, но формулировки темы и тела варьируй, чтобы письма не совпадали дословно."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "company": {
                                "homepage_excerpt": homepage_excerpt,
                            },
                            "guidelines": {
                                "language": self.language,
                                "avoid_marketing": True,
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }

    def _request_openai(self, payload: Dict[str, object]) -> Dict[str, object]:
        LOGGER.debug("Запрос к OpenAI: %s", payload)

        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
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
        subject = "Идея по оптимизации процессов вашей компании"
        industry_fragment = company.industry or "вашей сфере"
        if offer.value_proposition:
            automation_example = offer.value_proposition.lower()
            process_hint = (
                f"например, {automation_example}, чтобы команда меньше тратила времени на рутину"
            )
        elif offer.pains:
            pain_focus = offer.pains[0].lower()
            process_hint = (
                f"например, автоматизировать части процесса вокруг {pain_focus}, "
                "чтобы команда меньше тратила времени на рутину"
            )
        else:
            process_hint = (
                "например, автоматизировать обработку заявок или подготовку отчётов, "
                "чтобы команда меньше тратила времени на рутину"
            )
        observation = (
            "Обратил внимание, как вы последовательно развиваете проекты — глаз зацепился за кейсы на главной."
            if not offer.pains
            else "Понравилось, что вы так системно подходите к своим задачам — это редко встретишь."
        )
        body_lines = [
            "Добрый день!",
            "Меня зовут Марк, я занимаюсь автоматизацией бизнес-процессов с помощью нейросетей и Python.",
            f"Посмотрел ваш сайт — по описанию видно, что вы работаете в сфере {industry_fragment}.",
            observation,
            f"Мне кажется, здесь можно упростить процессы, {process_hint}.",
            "",
            "Если интересно, могу показать на конкретных примерах, как это работает.",
            "",
            "С уважением,",
            "Марк Аборчи",
            "AI-Automation Specialist",
        ]
        body = "\n".join(body_lines)
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
