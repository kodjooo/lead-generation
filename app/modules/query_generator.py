"""Генерация поисковых запросов и расписания для очереди SERP."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, List, Optional

DEFAULT_CONFIG = {
    "language": "ru",
    "night_window": {"start_utc": "20:00", "end_utc": "05:59"},
    "spacing_seconds": 45,
    "region_fallback_lr": 225,
    "max_queries_per_niche": 6,
    "triggers": [
        '"оставить заявку"',
        '"онлайн запись"',
        '"рассчитать стоимость"',
        '"коммерческое предложение"',
        '"бриф"',
    ],
    "neg_sites": [
        "avito.ru",
        "market.yandex.ru",
        "2gis.ru",
        "hh.ru",
        "flamp.ru",
        "otzovik.com",
        "irecommend.ru",
        "youtube.com",
        "vk.com",
        "reddit.com",
        "pikabu.ru",
    ],
    "regions_lr": {
        "Россия": 225,
        "Москва": 213,
        "Санкт-Петербург": 2,
        "Санкт‑Петербург": 2,
        "Новосибирск": 65,
    },
}


@dataclass
class NicheRow:
    """Исходные данные строки Google Sheets."""

    row_index: int
    niche: str
    city: Optional[str]
    country: Optional[str]
    batch_tag: Optional[str]


@dataclass
class GeneratedQuery:
    """Результат генерации одного запроса."""

    query_text: str
    query_hash: str
    region_code: int
    scheduled_for: datetime
    trigger: Optional[str]
    metadata: dict


class QueryGenerator:
    """Формирует поисковые запросы и расписание запуска."""

    def __init__(
        self,
        config: dict | None = None,
        *,
        now_func: callable = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config or DEFAULT_CONFIG
        self._now_func = now_func
        self._spacing = int(self.config.get("spacing_seconds", 45))
        self._max_queries = int(self.config.get("max_queries_per_niche", 6))
        self._language = self.config.get("language", "ru")
        night_cfg = self.config.get("night_window", {})
        self._window_start = self._parse_time(night_cfg.get("start_utc", "20:00"))
        self._window_end = self._parse_time(night_cfg.get("end_utc", "05:59"))
        self._neg_sites = list(self.config.get("neg_sites", []))
        self._triggers = list(self.config.get("triggers", []))
        self._regions_map = {
            self._normalize_key(k): v for k, v in self.config.get("regions_lr", {}).items()
        }
        self._region_fallback = int(self.config.get("region_fallback_lr", 225))

    @staticmethod
    def _parse_time(value: str) -> time:
        hours, minutes = value.split(":", 1)
        return time(int(hours), int(minutes), tzinfo=timezone.utc)

    @staticmethod
    def _normalize_key(value: str | None) -> str:
        return (value or "").strip().lower()

    def _resolve_region(self, city: Optional[str], country: Optional[str]) -> int:
        city_key = self._normalize_key(city)
        if city_key and city_key in self._regions_map:
            return self._regions_map[city_key]
        country_key = self._normalize_key(country)
        if country_key and country_key in self._regions_map:
            return self._regions_map[country_key]
        return self._region_fallback

    def _negatives(self) -> str:
        return " ".join(f"-site:{domain}" for domain in self._neg_sites)

    def _place_fragment(self, row: NicheRow) -> str:
        if row.city:
            return row.city.strip()
        if row.country:
            return row.country.strip()
        return ""

    def _build_queries_texts(self, row: NicheRow) -> List[tuple[str, Optional[str]]]:
        base_tokens = [f"lang:{self._language}", row.niche.strip()]
        place = self._place_fragment(row)
        if place:
            base_tokens.append(place)
        negatives = self._negatives()

        queries: List[tuple[str, Optional[str]]] = []
        base_query = " ".join(base_tokens) + (f" {negatives}" if negatives else "")
        queries.append((base_query, None))

        available_triggers = self._triggers[: max(0, self._max_queries - 1)]
        for trigger in available_triggers:
            tokens = list(base_tokens)
            tokens.append(trigger)
            query = " ".join(tokens) + (f" {negatives}" if negatives else "")
            queries.append((query, trigger))
            if len(queries) >= self._max_queries:
                break
        return queries

    def _window_bounds(self, reference_date) -> tuple[datetime, timedelta]:
        start_dt = datetime.combine(reference_date, self._window_start)
        end_dt = datetime.combine(reference_date, self._window_end)
        if self._window_end <= self._window_start:
            end_dt += timedelta(days=1)
        duration = end_dt - start_dt
        return start_dt, duration

    def _next_window_start(self, now: datetime) -> tuple[datetime, datetime]:
        start_today, duration = self._window_bounds(now.date())
        if self._window_end <= self._window_start and now < start_today:
            start_prev = start_today - timedelta(days=1)
            end_prev = start_prev + duration
            if start_prev <= now <= end_prev:
                return now, end_prev

        end_today = start_today + duration
        if start_today <= now <= end_today:
            return now, end_today
        if now < start_today:
            return start_today, end_today

        start_next = start_today + timedelta(days=1)
        end_next = start_next + duration
        return start_next, end_next

    def generate(self, row: NicheRow) -> List[GeneratedQuery]:
        """Формирует список запросов для строки листа."""
        queries_with_triggers = self._build_queries_texts(row)
        now = self._now_func()
        window_start, window_end = self._next_window_start(now)
        scheduled_times: List[datetime] = []
        for index, _ in enumerate(queries_with_triggers):
            scheduled = window_start + timedelta(seconds=self._spacing * index)
            if scheduled > window_end:
                break
            scheduled_times.append(scheduled)

        result: List[GeneratedQuery] = []
        region_code = self._resolve_region(row.city, row.country)
        metadata_base = {
            "niche": row.niche.strip(),
            "city": row.city.strip() if row.city else None,
            "country": row.country.strip() if row.country else None,
            "batch_tag": row.batch_tag.strip() if row.batch_tag else None,
            "language": self._language,
            "selection": "balanced",
        }

        for schedule_time, (query_text, trigger) in zip(scheduled_times, queries_with_triggers):
            cleaned = " ".join(query_text.split())
            query_hash = hashlib.sha1(f"{cleaned}|{region_code}".encode("utf-8"), usedforsecurity=False).hexdigest()
            metadata = dict(metadata_base)
            metadata["trigger"] = trigger
            result.append(
                GeneratedQuery(
                    query_text=cleaned,
                    query_hash=query_hash,
                    region_code=region_code,
                    scheduled_for=schedule_time,
                    trigger=trigger,
                    metadata=metadata,
                )
            )
        return result
