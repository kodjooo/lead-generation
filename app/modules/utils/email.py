"""Валидация и нормализация e-mail адресов."""

from __future__ import annotations

import re
from email.utils import parseaddr

EMAIL_REGEX = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)
STRIP_CHARS = "<>[]()\"' \t\r\n"


def clean_email(value: str) -> str:
    """Возвращает нормализованный e-mail без mailto/угловых скобок."""
    raw = (value or "").strip()
    if not raw:
        return ""

    lowered = raw.lower()
    if lowered.startswith("mailto:"):
        raw = raw.split(":", 1)[1]
    if "?" in raw:
        raw = raw.split("?", 1)[0]

    _, parsed = parseaddr(raw)
    candidate = parsed or raw
    candidate = candidate.strip(STRIP_CHARS)
    candidate = candidate.replace(" ", "")
    candidate = candidate.replace("\u200b", "")
    return candidate.lower()


def is_valid_email(value: str) -> bool:
    """Проверяет строку на соответствие базовым правилам RFC 5321."""
    candidate = clean_email(value)
    if not candidate or "@" not in candidate:
        return False
    return bool(EMAIL_REGEX.match(candidate))
