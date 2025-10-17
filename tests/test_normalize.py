"""Проверка функций нормализации URL и доменов."""

from app.modules.utils.normalize import (
    build_company_dedupe_key,
    clean_snippet,
    normalize_domain,
    normalize_url,
)


def test_normalize_url_adds_scheme_and_trims_path() -> None:
    assert normalize_url("example.com") == "https://example.com/"
    assert normalize_url("HTTP://WWW.test.ru/path//") == "http://test.ru/path"


def test_normalize_domain_handles_punycode() -> None:
    assert normalize_domain("https://WWW.Example.com/ru") == "example.com"
    assert normalize_domain("тест.рф") == "xn--e1aybc.xn--p1ai"


def test_build_company_dedupe_key_stable() -> None:
    key1 = build_company_dedupe_key("Test", "example.com")
    key2 = build_company_dedupe_key("Другое имя", "example.com")
    assert key1 == key2


def test_clean_snippet_compacts_whitespace() -> None:
    assert clean_snippet("  Привет\nмир  ") == "Привет мир"
