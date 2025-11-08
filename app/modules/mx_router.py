"""Маршрутизация SMTP по MX-записям домена."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

import dns.exception
import dns.resolver

from app.config import RoutingSettings, get_settings

LOGGER = logging.getLogger("app.mx_router")


@dataclass(frozen=True)
class MXResult:
    """Результат классификации MX-записей."""

    classification: str  # RU | OTHER | UNKNOWN
    records: List[str]
    ttl_hit: bool


class TTLCache:
    """Простейший LRU+TTL кэш для MX-записей."""

    def __init__(self, ttl_seconds: int, maxsize: int = 1024) -> None:
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._store: OrderedDict[str, Tuple[float, Tuple[str, List[str]]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Tuple[str, List[str]]]:
        now = time.time()
        with self._lock:
            payload = self._store.get(key)
            if not payload:
                return None
            expires_at, value = payload
            if expires_at <= now:
                self._store.pop(key, None)
                return None
            # перемещаем в конец, чтобы поддерживать LRU
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Tuple[str, List[str]]) -> None:
        expires_at = time.time() + self._ttl
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (expires_at, value)
            if len(self._store) > self._maxsize:
                self._store.popitem(last=False)


class MXRouter:
    """Определяет SMTP-провайдера на основе MX-записей домена."""

    def __init__(
        self,
        settings: Optional[RoutingSettings] = None,
        *,
        cache: Optional[TTLCache] = None,
        resolver: Optional[dns.resolver.Resolver] = None,
    ) -> None:
        self.settings = settings or get_settings().routing
        ttl_seconds = max(self.settings.mx_cache_ttl_hours * 3600, 60)
        self._cache = cache or TTLCache(ttl_seconds)
        self._resolver = resolver
        self._resolvers_order = self._build_resolver_order(self.settings.dns_resolvers)
        self._ru_patterns = tuple(p.lower() for p in self.settings.ru_mx_patterns if p)
        self._force_ru_domains = {domain.lower() for domain in self.settings.force_ru_domains if domain}

    def classify(self, domain: str) -> MXResult:
        """Возвращает класс MX-домена и список MX-записей."""
        if not self.settings.enabled:
            return MXResult(classification="OTHER", records=[], ttl_hit=False)

        normalized = (domain or "").strip().lower()
        if not normalized:
            LOGGER.warning("Получен пустой домен для MX-классификации.")
            return MXResult(classification="UNKNOWN", records=[], ttl_hit=False)

        cache_key = f"mx:{normalized}"
        if normalized in self._force_ru_domains:
            self._cache.set(cache_key, ("RU", []))
            return MXResult(classification="RU", records=[], ttl_hit=False)

        cached = self._cache.get(cache_key)
        if cached:
            classification, records = cached
            return MXResult(classification=classification, records=list(records), ttl_hit=True)

        classification, records = self._classify_uncached(normalized)
        self._cache.set(cache_key, (classification, records))
        return MXResult(classification=classification, records=list(records), ttl_hit=False)

    def _classify_uncached(self, domain: str) -> Tuple[str, List[str]]:
        try:
            records = self._resolve_mx(domain)
        except dns.exception.DNSException as exc:
            LOGGER.warning("MX lookup failed for %s: %s", domain, exc)
            return "UNKNOWN", []

        if not records:
            LOGGER.info("MX lookup returned no records for %s.", domain)
            return "UNKNOWN", []

        if self._matches_ru(records):
            return "RU", records
        return "OTHER", records

    def _matches_ru(self, records: Iterable[str]) -> bool:
        for record in records:
            lowered = record.lower()
            if any(pattern in lowered for pattern in self._ru_patterns):
                return True
        return False

    def _resolve_mx(self, domain: str) -> List[str]:
        attempts = 0
        last_error: Optional[Exception] = None
        start = time.perf_counter()
        for nameservers in self._resolvers_order:
            attempts += 1
            resolver = self._resolver or dns.resolver.Resolver(configure=not nameservers)
            resolver.timeout = self.settings.dns_timeout_seconds
            resolver.lifetime = self.settings.dns_timeout_seconds
            if nameservers:
                resolver.nameservers = list(nameservers)

            try:
                LOGGER.debug("Resolving MX for %s via %s", domain, nameservers or "system")
                answers = resolver.resolve(domain, "MX")
                latency_ms = int((time.perf_counter() - start) * 1000)
                hosts = [str(r.exchange).rstrip(".").lower() for r in answers]
                LOGGER.info(
                    "Resolved MX for %s (%dms): %s",
                    domain,
                    latency_ms,
                    ", ".join(hosts) or "<empty>",
                )
                return hosts
            except dns.exception.DNSException as exc:
                LOGGER.warning("Attempt %d to resolve MX for %s failed: %s", attempts, domain, exc)
                last_error = exc
                if attempts >= 2:
                    break

        if last_error:
            raise last_error
        return []

    @staticmethod
    def _build_resolver_order(resolvers: Sequence[str]) -> List[Tuple[str, ...]]:
        filtered = [resolver.strip() for resolver in resolvers if resolver.strip()]
        order: List[Tuple[str, ...]] = []
        if filtered:
            order.append(tuple(filtered))
        # Вторая попытка — системные настройки
        order.append(tuple())
        return order

