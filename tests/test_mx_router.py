"""Тесты для MXRouter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import List

import dns.exception
import pytest

from app.config import RoutingSettings
from app.modules.mx_router import MXResult, MXRouter, TTLCache


class FakeResolver:
    """Заглушка резолвера, имитирует ответы на MX-запросы."""

    def __init__(self, responses: List[object]) -> None:
        self._responses = responses
        self.calls = 0
        self.nameservers: List[str] = []
        self.timeout = 0.0
        self.lifetime = 0.0

    def resolve(self, domain: str, record_type: str) -> object:
        if not self._responses:
            raise dns.exception.DNSException("no more responses")
        self.calls += 1
        value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def routing_settings(**overrides) -> RoutingSettings:
    base = {
        "enabled": True,
        "mx_cache_ttl_hours": 1,
        "dns_timeout_seconds": 0.5,
        "dns_resolvers": ("1.1.1.1", "8.8.8.8"),
        "ru_mx_patterns": ("mx.yandex.net", "mx.mail.ru"),
        "ru_mx_tlds": (".ru",),
        "force_ru_domains": ("mail.ru",),
    }
    base.update(overrides)
    return RoutingSettings(**base)


def test_force_domain_returns_ru() -> None:
    router = MXRouter(routing_settings(), cache=TTLCache(60))

    result = router.classify("mail.ru")

    assert result.classification == "RU"
    assert result.records == []
    assert result.ttl_hit is False


def test_classification_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = [
        [SimpleNamespace(exchange="mx.yandex.net.")],
    ]
    resolver = FakeResolver(responses=answers.copy())
    router = MXRouter(routing_settings(), cache=TTLCache(60), resolver=resolver)

    first = router.classify("example.ru")
    second = router.classify("example.ru")

    assert first.classification == "RU"
    assert first.ttl_hit is False
    assert second.classification == "RU"
    assert second.ttl_hit is True
    assert resolver.calls == 1


def test_classification_returns_unknown_after_failures() -> None:
    responses = [
        dns.exception.Timeout(),
        dns.exception.Timeout(),
    ]
    resolver = FakeResolver(responses=responses)  # type: ignore[arg-type]
    router = MXRouter(routing_settings(), cache=TTLCache(60), resolver=resolver)

    result = router.classify("unreachable.example")

    assert result.classification == "UNKNOWN"
    assert result.records == []


def test_detects_ru_by_tld() -> None:
    responses = [
        [SimpleNamespace(exchange="mail.company.ru.")],
    ]
    resolver = FakeResolver(responses=responses.copy())
    router = MXRouter(
        routing_settings(ru_mx_patterns=tuple(), ru_mx_tlds=(".ru", ".su")),
        cache=TTLCache(60),
        resolver=resolver,
    )

    result = router.classify("company.ru")

    assert result.classification == "RU"
    assert result.records == ["mail.company.ru"]


def test_other_when_tld_not_matched() -> None:
    responses = [
        [SimpleNamespace(exchange="aspmx.l.google.com.")],
    ]
    resolver = FakeResolver(responses=responses.copy())
    router = MXRouter(
        routing_settings(ru_mx_patterns=tuple(), ru_mx_tlds=(".ru",)),
        cache=TTLCache(60),
        resolver=resolver,
    )

    result = router.classify("company.com")

    assert result.classification == "OTHER"
    assert result.records == ["aspmx.l.google.com"]
