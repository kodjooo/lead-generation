"""Генерация поисковых запросов и расписания для очереди SERP."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Iterable, List, Optional

DEFAULT_CONFIG = {
    "language": "ru",
    "night_window": {"start_local": "00:00", "end_local": "07:59", "timezone": "Europe/Moscow"},
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
        "domain:avito.ru",
        "yandex.ru",
        "2gis.ru",
        "hh.ru",
        "flamp.ru",
        "otzovik.com",
        "irecommend.ru",
        "youtube.com",
        "profi.ru",
        "yell.ru",
        "workspace.ru",
        "vuzopedia.ru",
        "orgpage.ru",
        "rating-gamedev.ru",
        "ru.wadline.com",
        "vk.com",
        "reddit.com",
        "pikabu.ru",
    ],
    "regions_lr": {
        "россия": 225,
        "москва и московская область": 1,
        "москва": 213,
        "санкт‑петербург": 2,
        "saint petersburg": 2,
        "архангельск": 20,
        "nazran": 1092,
        "назрань": 1092,
        "астрахань": 37,
        "nalchik": 30,
        "нальчик": 30,
        "barnaul": 197,
        "барнаул": 197,
        "нижний новгород": 47,
        "belgorod": 4,
        "белгород": 4,
        "новосибирск": 65,
        "blagoveshchensk": 77,
        "благовещенск": 77,
        "омск": 66,
        "bryansk": 191,
        "брянск": 191,
        "орёл": 10,
        "орел": 10,
        "veliky novgorod": 24,
        "великий новгород": 24,
        "оренбург": 48,
        "владивосток": 75,
        "penza": 49,
        "пенза": 49,
        "владикавказ": 33,
        "perm": 50,
        "пермь": 50,
        "vladimir": 192,
        "владимир": 192,
        "псков": 25,
        "волгоград": 38,
        "rostov-on-don": 39,
        "ростов-на-дону": 39,
        "вологда": 21,
        "ryazan": 11,
        "рязань": 11,
        "voronezh": 193,
        "воронеж": 193,
        "samara": 51,
        "самара": 51,
        "grozny": 1106,
        "грозный": 1106,
        "yekaterinburg": 54,
        "екатеринбург": 54,
        "saransk": 42,
        "саранск": 42,
        "ivanovo": 5,
        "иваново": 5,
        "smolensk": 12,
        "смоленск": 12,
        "irkutsk": 63,
        "irkutsk oblast": 63,
        "irkutskaya oblast": 63,
        "иркутск": 63,
        "сочи": 239,
        "yoshkar-ola": 41,
        "йошкар-ола": 41,
        "stavropol": 36,
        "ставрополь": 36,
        "kazan": 43,
        "казань": 43,
        "surgut": 973,
        "сургут": 973,
        "kaliningrad": 22,
        "калининград": 22,
        "tambov": 13,
        "тамбов": 13,
        "kemerovo": 64,
        "кемерово": 64,
        "tver": 14,
        "тверь": 14,
        "kostroma": 7,
        "кострома": 7,
        "tomsk": 67,
        "томск": 67,
        "krasnodar": 35,
        "краснодар": 35,
        "tula": 15,
        "тула": 15,
        "krasnoyarsk": 62,
        "красноярск": 62,
        "ulyanovsk": 195,
        "ульяновск": 195,
        "kurgan": 53,
        "курган": 53,
        "ufa": 172,
        "уфа": 172,
        "kursk": 8,
        "курск": 8,
        "khabarovsk": 76,
        "хабаровск": 76,
        "lipetsk": 9,
        "липецк": 9,
        "cheboksary": 45,
        "чебоксары": 45,
        "makhachkala": 28,
        "махачкала": 28,
        "chelyabinsk": 56,
        "челябинск": 56,
        "cherkessk": 1104,
        "черкесск": 1104,
        "yaroslavl": 16,
        "ярославль": 16,
        "murmansk": 23,
        "мурманск": 23,
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
        self._night_tz = ZoneInfo(night_cfg.get("timezone", "UTC"))
        self._window_start_local = self._parse_time(night_cfg.get("start_local", "00:00"))
        self._window_end_local = self._parse_time(night_cfg.get("end_local", "07:59"))
        self._neg_sites = list(self.config.get("neg_sites", []))
        self._triggers = list(self.config.get("triggers", []))
        self._regions_map = {
            self._normalize_key(k): v for k, v in self.config.get("regions_lr", {}).items()
        }
        self._region_fallback = int(self.config.get("region_fallback_lr", 225))

    @staticmethod
    def _parse_time(value: str) -> time:
        hours, minutes = value.split(":", 1)
        return time(int(hours), int(minutes))

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
        tokens: List[str] = []
        for entry in self._neg_sites:
            raw = entry.strip()
            if not raw:
                continue
            if ":" in raw:
                prefix, value = raw.split(":", 1)
                prefix = prefix.strip().lower()
                value = value.strip()
                if prefix in {"site", "domain", "host"} and value:
                    tokens.append(f"-{prefix}:{value}")
                    continue
            tokens.append(f"-site:{raw}")
        return " ".join(tokens)

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
        start_local = datetime.combine(reference_date, self._window_start_local, self._night_tz)
        end_local = datetime.combine(reference_date, self._window_end_local, self._night_tz)
        if self._window_end_local <= self._window_start_local:
            end_local += timedelta(days=1)
        duration = end_local - start_local
        return start_local.astimezone(timezone.utc), duration

    def _next_window_start(self, now: datetime) -> tuple[datetime, datetime]:
        start_today, duration = self._window_bounds(now.date())
        if self._window_end_local <= self._window_start_local and now < start_today:
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
