#!/usr/bin/env python3
"""Сбор MX-записей популярных российских доменов для обновления ROUTING_RU_MX_PATTERNS."""

from __future__ import annotations

import json
from collections import OrderedDict, defaultdict
from typing import Dict, Iterable, List, Sequence, Set, Tuple

import dns.exception
import dns.resolver

# Топ доменов: крупные медиа, банки, ритейл, госслужбы и хостеры.
SEED_DOMAINS: Tuple[str, ...] = (
    "yandex.ru",
    "ya.ru",
    "mail.ru",
    "bk.ru",
    "inbox.ru",
    "list.ru",
    "rambler.ru",
    "lenta.ru",
    "gazeta.ru",
    "kommersant.ru",
    "rbc.ru",
    "vc.ru",
    "vedomosti.ru",
    "sostav.ru",
    "proactivity.ru",
    "gosuslugi.ru",
    "sber.ru",
    "sberbank.ru",
    "tbank.ru",
    "tinkoff.ru",
    "wildberries.ru",
    "ozon.ru",
    "hh.ru",
    "pochta.ru",
    "russianpost.ru",
    "runity.ru",
    "timeweb.ru",
    "mchost.ru",
    "spaceweb.ru",
    "beget.ru",
    "beget.com",
    "reg.ru",
    "nic.ru",
    "selectel.ru",
    "selectel.org",
    "netangels.ru",
    "sprinthost.ru",
    "masterhost.ru",
    "1c.ru",
    "aeroflot.ru",
    "vtb.ru",
    "vtb.com",
    "alfabank.ru",
    "sovcombank.ru",
    "rosatom.ru",
    "roscosmos.ru",
    "mos.ru",
    "nornickel.ru",
    "magnit.ru",
    "x5.ru",
    "lukoil.ru",
    "lukoil.com",
    "tatneft.ru",
    "gazprom.ru",
    "novatek.ru",
    "megafon.ru",
    "mts.ru",
    "beeline.ru",
    "rt.ru",
    "facct.ru",
    "facct.email",
    "lancloud.ru",
    "sevstar.net",
)


def base_zone(hostname: str) -> str:
    """Возвращает базовый домен (пример: mx3.timeweb.ru -> timeweb.ru)."""
    chunks = hostname.split(".")
    if len(chunks) < 2:
        return hostname
    candidate = ".".join(chunks[-2:])
    if chunks[-2] in {"co", "com", "org", "net"} and len(chunks) >= 3 and chunks[-1] in {"ru", "su"}:
        candidate = ".".join(chunks[-3:])
    return candidate


def resolve_mx(domains: Sequence[str]) -> Tuple[Dict[str, Set[str]], Set[str]]:
    """Собирает MX-хосты и базовые домены."""
    resolver = dns.resolver.Resolver()
    mx_hosts: Dict[str, Set[str]] = defaultdict(set)
    zones: Set[str] = set()

    for domain in domains:
        try:
            answers = resolver.resolve(domain, "MX")
        except dns.exception.DNSException as exc:
            print(f"[warn] MX lookup failed for {domain}: {exc}")
            continue
        for record in answers:
            host = str(record.exchange).rstrip(".").lower()
            if not host:
                continue
            mx_hosts[host].add(domain)
            zones.add(base_zone(host))

    return mx_hosts, zones


def main() -> None:
    mx_hosts, zones = resolve_mx(SEED_DOMAINS)
    ordered_hosts = OrderedDict(sorted(mx_hosts.items()))

    print("# MX-хосты → список доменов, где они встретились")
    print(json.dumps({host: sorted(domains) for host, domains in ordered_hosts.items()}, ensure_ascii=False, indent=2))
    print()
    print("# Базовые домены (предлагаемые паттерны)")
    print(",".join(sorted(zones)))


if __name__ == "__main__":
    main()
