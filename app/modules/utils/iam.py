"""Получение IAM токенов Yandex Cloud по ключу сервисного аккаунта."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import jwt

LOGGER = logging.getLogger("app.iam")
TOKEN_ENDPOINT = "https://iam.api.cloud.yandex.net/iam/v1/tokens"


@dataclass
class ServiceAccountKey:
    """Данные ключа сервисного аккаунта."""

    service_account_id: str
    key_id: str
    private_key: str
    key_algorithm: str


class IamTokenProvider:
    """Генерирует и кеширует IAM токены на основе ключа сервисного аккаунта."""

    def __init__(
        self,
        *,
        key: ServiceAccountKey,
        http_client: Optional[httpx.Client] = None,
        refresh_margin: int = 60,
    ) -> None:
        self._key = key
        self._http_client = http_client or httpx.Client(timeout=10.0)
        self._refresh_margin = refresh_margin
        self._cached_token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        """Возвращает актуальный IAM токен, обновляя его при необходимости."""
        now = time.time()
        if self._cached_token and now < (self._expires_at - self._refresh_margin):
            return self._cached_token

        jwt_assertion = self._build_jwt(now)
        response = self._http_client.post(TOKEN_ENDPOINT, json={"jwt": jwt_assertion})
        if response.status_code >= 400:
            LOGGER.error("Ошибка получения IAM токена: %s %s", response.status_code, response.text)
            raise RuntimeError("Не удалось получить IAM токен")

        payload = response.json()
        token = payload.get("iamToken")
        expires_at = payload.get("expiresAt")
        if not token or not expires_at:
            raise RuntimeError("Ответ IAM API не содержит токен")

        # expiresAt в RFC3339, преобразуем в UNIX-время
        self._cached_token = token
        self._expires_at = self._parse_expiration(expires_at)
        LOGGER.debug("IAM токен обновлён, истекает в %s", expires_at)
        return token

    def _build_jwt(self, now: float) -> str:
        algorithm = "PS256" if "RSA" in self._key.key_algorithm else "ES256"
        headers = {"kid": self._key.key_id}
        payload = {
            "aud": TOKEN_ENDPOINT,
            "iss": self._key.service_account_id,
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        return jwt.encode(payload, self._key.private_key, algorithm=algorithm, headers=headers)

    @staticmethod
    def _parse_expiration(expires_at: str) -> float:
        from datetime import datetime

        normalized = expires_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.timestamp()


def load_service_account_key_from_file(path: Path) -> ServiceAccountKey:
    """Читает ключ сервисного аккаунта из файла JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return ServiceAccountKey(
        service_account_id=data["service_account_id"],
        key_id=data["id"],
        private_key=data["private_key"],
        key_algorithm=data.get("key_algorithm", "RSA_2048"),
    )


def load_service_account_key_from_string(raw: str) -> ServiceAccountKey:
    """Читает ключ сервисного аккаунта из JSON-строки."""
    data = json.loads(raw)
    return ServiceAccountKey(
        service_account_id=data["service_account_id"],
        key_id=data["id"],
        private_key=data["private_key"],
        key_algorithm=data.get("key_algorithm", "RSA_2048"),
    )


class StaticTokenProvider:
    """Простой провайдер, возвращающий заранее заданный токен."""

    def __init__(self, token: str) -> None:
        self._token = token

    def get_token(self) -> str:
        return self._token
