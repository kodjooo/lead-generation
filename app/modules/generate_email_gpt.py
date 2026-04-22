"""Генерация персонализированных писем с помощью LLM."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from app.config import get_settings

LOGGER = logging.getLogger("app.generate_email")
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_GENERATION_RETRIES = 3
DEFAULT_RETRY_DELAYS_SECONDS = (20, 40, 60)


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


class EmailGenerationError(RuntimeError):
    """Ошибка генерации письма после всех попыток."""


class EmailGenerator:
    """Инкапсулирует обращение к LLM."""

    def __init__(
        self,
        *,
        model: str | None = None,
        language: str = "ru",
        temperature: float = 0.4,
        timeout: float = 60.0,
        retry_attempts: int = DEFAULT_GENERATION_RETRIES,
        retry_delays_seconds: tuple[int, ...] = DEFAULT_RETRY_DELAYS_SECONDS,
        reasoning_effort: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.model = model or self.settings.openai_email_model
        self.reasoning_effort = reasoning_effort or self.settings.openai_reasoning_effort
        self.language = language
        self.temperature = temperature
        self.timeout = timeout
        self.retry_attempts = max(1, retry_attempts)
        self.retry_delays_seconds = retry_delays_seconds

    def generate(
        self,
        company: CompanyBrief,
        offer: OfferBrief,
        contact: Optional[ContactBrief] = None,
    ) -> GeneratedEmail:
        """Возвращает готовый шаблон и исходный запрос к LLM."""
        payload: Optional[Dict[str, object]] = None
        if not self.settings.openai_api_key:
            raise EmailGenerationError("OPENAI_API_KEY не задан")

        payload = self._build_payload(company, offer, contact)
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                response = self._request_openai(payload)
                parsed = self._parse_openai_response(response)
                if parsed:
                    return GeneratedEmail(template=parsed, request_payload=payload, used_fallback=False)
                last_error = EmailGenerationError("OpenAI returned an empty or invalid payload")
            except httpx.HTTPError as exc:  # noqa: PERF203
                last_error = exc

            if attempt < self.retry_attempts:
                LOGGER.warning("Ошибка генерации письма, попытка %s/%s: %s", attempt, self.retry_attempts, last_error)
                delay_index = min(attempt - 1, len(self.retry_delays_seconds) - 1)
                if delay_index >= 0:
                    time.sleep(self.retry_delays_seconds[delay_index])

        raise EmailGenerationError(f"Не удалось сгенерировать письмо после {self.retry_attempts} попыток: {last_error}")

    def _build_payload(
        self,
        company: CompanyBrief,
        offer: OfferBrief,
        contact: Optional[ContactBrief],
    ) -> Dict[str, object]:
        homepage_excerpt = " ".join(company.highlights) if company.highlights else None
        return {
            "model": self.model,
            "reasoning": {"effort": self.reasoning_effort},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "EmailTemplate",
                    "schema": self._response_schema(),
                    "strict": True,
                }
            },
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Ты Марк Аборчи, специалист по AI-автоматизации. Твоя задача — писать "
                                "персонализированные, человеческие письма на русском языке для компаний, "
                                "которым можно помочь автоматизацией процессов с помощью нейросетей и Python. "
                                "Избегай рекламного тона и превосходных степеней. Делай акцент на пользе: экономия времени, "
                                "сокращение затрат, устранение рутины, повышение эффективности. Письмо должно быть лаконичным: "
                                "примерно 120-180 слов, без длинных списков и перегруженных объяснений. "
                                "Всегда используй JSON-ответ с полями subject и body. "
                                "Структура письма фиксирована: тема передаёт идею оптимизации процессов компании (например, 'Идея по оптимизации процессов вашей компании') и тело состоит из блоков:\n"
                                "1) Приветствие 'Добрый день!'.\n"
                                "2) Короткое представление Марка и его подхода (нейросети, Python).\n"
                                "3) Очень короткое и естественное упоминание, чем занимается компания: 1-2 предложения максимум, без перегруженного перечисления всех услуг. Добавь одно короткое наблюдение о том, что показалось сильной стороной.\n"
                                "4) Описание одного конкретного процесса, который можно упростить с помощью AI, и ожидаемого эффекта. Пиши как живой человек, без канцелярских конструкций вроде 'Предложение:' или 'Предлагаю упростить два процесса:'.\n"
                                "5) Короткое, нейтральное и человеческое приглашение к диалогу: можно обсудить, уместна ли эта идея, как подобный сценарий мог бы выглядеть в бизнесе компании, и если нет, то какие ещё варианты автоматизации могли бы быть полезны. Не обещай существующие кейсы, которых может не быть, и не используй формулировки про 'показать примеры' или 'показать, как это работает'.\n"
                                "6) Завершение: 'С уважением,' + имя и должность.\n"
                                "Не используй маркированные списки, если без них можно обойтись. "
                                "Структуру сохраняй, но формулировки темы и тела варьируй, чтобы письма не совпадали дословно."
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(
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
                        }
                    ],
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
            response = client.post(OPENAI_RESPONSES_URL, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    def _parse_openai_response(self, response: Dict[str, object]) -> Optional[EmailTemplate]:
        try:
            content = response.get("output_text")
            if not content:
                output_items = response.get("output", [])
                for item in output_items if isinstance(output_items, list) else []:
                    if not isinstance(item, dict) or item.get("type") != "message":
                        continue
                    for part in item.get("content", []):
                        if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text"):
                            content = part["text"]
                            break
                    if content:
                        break
            if not content:
                return None
            parsed = json.loads(content)
            return EmailTemplate(subject=parsed["subject"], body=parsed["body"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            LOGGER.error("Не удалось интерпретировать ответ LLM: %s", response)
            return None

    def _response_schema(self) -> Dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["subject", "body"],
            "additionalProperties": False,
        }
