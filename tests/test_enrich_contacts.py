"""Тесты обогащения контактами."""

import json
from types import SimpleNamespace

from app.modules.enrich_contacts import ContactEnricher


class DummyResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one(self) -> str:
        return self._value


class DummySession:
    def __init__(self) -> None:
        self.calls = []
        self.counter = 0

    def execute(self, statement, params):  # noqa: ANN001
        sql = statement.text if hasattr(statement, "text") else str(statement)
        self.calls.append((sql, params))
        if "INSERT INTO contacts" in sql:
            self.counter += 1
            return DummyResult(f"contact-{self.counter}")
        return DummyResult("noop")

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_extract_contacts_from_html() -> None:
    enricher = ContactEnricher(session_factory=lambda: None)  # type: ignore[arg-type]
    html = """
    <html>
      <body>
        <a href="mailto:sales@example.com">Sales</a>
        <a href="tel:+7 (495) 123-45-67">Позвонить</a>
        <p>Общий e-mail: info@example.com</p>
        <p>Телефон офиса: +7 812 000-11-22</p>
      </body>
    </html>
    """

    contacts = list(enricher._extract_contacts_from_html(html, "https://example.com"))
    emails = [c for c in contacts if c.contact_type == "email"]
    assert len(emails) == 1
    assert emails[0].value.lower() == "sales@example.com"


def test_extract_contacts_skips_invalid_mailto() -> None:
    enricher = ContactEnricher(session_factory=lambda: None)  # type: ignore[arg-type]
    html = """
    <html>
      <body>
        <a href="mailto:+74951234567">Позвонить</a>
      </body>
    </html>
    """

    contacts = list(enricher._extract_contacts_from_html(html, "https://example.com"))
    assert contacts == []


def test_extract_contacts_from_text_without_mailto() -> None:
    enricher = ContactEnricher(session_factory=lambda: None)  # type: ignore[arg-type]
    html = "<html><body><p>Для связи: info@example.com</p></body></html>"

    contacts = list(enricher._extract_contacts_from_html(html, "https://example.com"))

    assert len(contacts) == 1
    assert contacts[0].value == "info@example.com"
    assert contacts[0].origin == "text"


def test_extract_contacts_decodes_percent_encoded_email() -> None:
    enricher = ContactEnricher(session_factory=lambda: None)  # type: ignore[arg-type]
    html = "<html><body><p>Связь: %20info@jurint.pro</p></body></html>"

    contacts = list(enricher._extract_contacts_from_html(html, "https://example.com"))

    assert len(contacts) == 1
    assert contacts[0].value == "info@jurint.pro"


def test_enrich_company_persists_contacts(monkeypatch) -> None:
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    monkeypatch.setattr(
        enricher,
        "_fetch_html",
        lambda url: """
            <html>
              <body>
                <h1>Digital агентство</h1>
                <a href=\"mailto:HELLO@site.com\">Напишите нам</a>
                <a href=\"tel:+7 (900) 123-45-67\">Позвонить</a>
                <p>Резервный e-mail: Sales@site.com</p>
              </body>
            </html>
        """,
    )

    inserted = enricher.enrich_company("company-1", "site.com", session=session)

    assert inserted == ["contact-1"]
    assert "UPDATE companies" in session.calls[0][0]
    insert_calls = [call for call in session.calls if "INSERT INTO contacts" in call[0]]
    assert len(insert_calls) == 1
    assert insert_calls[0][1]["value"] == "hello@site.com"
    assert insert_calls[0][1]["is_primary"] is True


def test_enrich_company_uses_contact_page_text_email(monkeypatch) -> None:
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    pages = {
        "https://site.com/": "<html><body><h1>Главная</h1></body></html>",
        "https://site.com/contact": "<html><body><p>Связаться можно по адресу office@site.com</p></body></html>",
    }
    monkeypatch.setattr(enricher, "_fetch_html", lambda url: pages.get(url, ""))

    inserted = enricher.enrich_company("company-4", "site.com", session=session)

    assert inserted == ["contact-1"]
    insert_calls = [call for call in session.calls if "INSERT INTO contacts" in call[0]]
    assert len(insert_calls) == 1
    assert insert_calls[0][1]["value"] == "office@site.com"


def test_enrich_company_marks_not_found(monkeypatch) -> None:
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    monkeypatch.setattr(
        enricher,
        "_fetch_html",
        lambda url: "<html><body><h1>О компании</h1><p>Без явных контактных email.</p></body></html>",
    )

    inserted = enricher.enrich_company("company-2", "empty.com", session=session)

    assert inserted == []
    status_calls = [call for call in session.calls if "SET status" in call[0]]
    assert status_calls
    assert status_calls[-1][1]["status"] == "contacts_not_found"


def test_sanitize_excerpt_removes_control_chars() -> None:
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    dirty_html = "<html><body>Привет\u0000 мир\u0008!</body></html>"
    enricher._save_homepage_excerpt(session, "company-3", dirty_html)

    update_call = next(call for call in session.calls if "UPDATE companies" in call[0])
    payload = update_call[1]["patch"]
    data = json.loads(payload)
    assert data["homepage_excerpt"] == "Привет мир!"
    assert "\u0000" not in data["homepage_excerpt"]


def test_proxy_rotation_picks_one_of_configured_proxies(monkeypatch) -> None:
    monkeypatch.setenv("ENRICH_PROXY_URL", "http://proxy1.local:8080,http://proxy2.local:8080,http://proxy3.local:8080")
    enricher = ContactEnricher(session_factory=lambda: None)  # type: ignore[arg-type]

    clients = enricher._clients_for_url("https://example.com/contact")
    assert clients[0] is None
    assert len(enricher.proxy_urls) == 3


def test_enrich_company_retries_next_proxy_on_error(monkeypatch) -> None:
    monkeypatch.setenv("ENRICH_PROXY_URL", "http://proxy1.local:8080,http://proxy2.local:8080")
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    calls = []

    def fake_load_page(url: str, proxy_url: str | None):  # noqa: ANN001
        calls.append(proxy_url)
        if proxy_url is None:
            return SimpleNamespace(status=503, html="")
        return SimpleNamespace(status=200, html="<html><body>info@example.com</body></html>")

    monkeypatch.setattr(enricher, "_load_page", fake_load_page)

    inserted = enricher.enrich_company("company-5", "example.com", session=session)

    assert inserted == ["contact-1"]
    assert calls[0] is None
    assert calls[1] is not None


def test_enrich_company_rotates_proxy_on_429(monkeypatch) -> None:
    monkeypatch.setenv("ENRICH_PROXY_URL", "http://proxy1.local:8080,http://proxy2.local:8080")
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    calls = []

    def fake_load_page(url: str, proxy_url: str | None):  # noqa: ANN001
        calls.append(proxy_url)
        if proxy_url is None:
            return SimpleNamespace(status=429, html="")
        return SimpleNamespace(status=200, html="<html><body>info@example.com</body></html>")

    monkeypatch.setattr(enricher, "_load_page", fake_load_page)

    inserted = enricher.enrich_company("company-6", "example.com", session=session)

    assert inserted == ["contact-1"]
    assert calls[0] is None
    assert calls[1] is not None


def test_load_page_does_not_reuse_context_after_playwright_shutdown(monkeypatch) -> None:
    enricher = ContactEnricher(session_factory=lambda: None)  # type: ignore[arg-type]

    created_contexts = []

    class FakePage:
        def add_init_script(self, script: str) -> None:
            return None

        def set_extra_http_headers(self, headers):  # noqa: ANN001
            return None

        def goto(self, url: str, wait_until: str, timeout: int):  # noqa: ARG002
            return SimpleNamespace(status=200)

        def wait_for_timeout(self, timeout_ms: int) -> None:  # noqa: ARG002
            return None

        def content(self) -> str:
            return "<html><body>info@example.com</body></html>"

        def close(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.closed = False

        def new_page(self) -> FakePage:
            if self.closed:
                raise RuntimeError("context already closed")
            return FakePage()

        def close(self) -> None:
            self.closed = True

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):  # noqa: ANN003, ARG002
            context = FakeContext()
            created_contexts.append(context)
            return context

    class FakePlaywrightManager:
        def __enter__(self):
            return SimpleNamespace(chromium=FakeChromium())

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

    monkeypatch.setattr(enricher, "_get_playwright", lambda: FakePlaywrightManager())

    first = enricher._load_page("https://example.com", None)
    second = enricher._load_page("https://example.com/contact", None)

    assert first.status == 200
    assert second.status == 200
    assert len(created_contexts) == 2
    assert all(context.closed for context in created_contexts)
