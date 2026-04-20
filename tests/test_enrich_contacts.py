"""Тесты обогащения контактами."""

import json

import httpx
import respx

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
    html = """
    <html>
      <body>
        <p>Для связи: info@example.com</p>
      </body>
    </html>
    """

    contacts = list(enricher._extract_contacts_from_html(html, "https://example.com"))

    assert len(contacts) == 1
    assert contacts[0].value == "info@example.com"
    assert contacts[0].origin == "text"


def test_extract_contacts_decodes_percent_encoded_email() -> None:
    enricher = ContactEnricher(session_factory=lambda: None)  # type: ignore[arg-type]
    html = """
    <html>
      <body>
        <p>Связь: %20info@jurint.pro</p>
      </body>
    </html>
    """

    contacts = list(enricher._extract_contacts_from_html(html, "https://example.com"))

    assert len(contacts) == 1
    assert contacts[0].value == "info@jurint.pro"


@respx.mock
def test_enrich_company_persists_contacts() -> None:
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    respx.get("https://site.com/").mock(
        return_value=httpx.Response(
            200,
            text="""
            <html>
              <body>
                <h1>Digital агентство</h1>
                <a href=\"mailto:HELLO@site.com\">Напишите нам</a>
                <a href=\"tel:+7 (900) 123-45-67\">Позвонить</a>
                <p>Резервный e-mail: Sales@site.com</p>
                <p>Телефон офиса: 8 800 555 35 35</p>
                <p>Иностранный номер: +1 202 555 0199</p>
              </body>
            </html>
            """,
        )
    )

    inserted = enricher.enrich_company("company-1", "site.com", session=session)

    assert inserted == ["contact-1"]
    # первый вызов — обновление companies с homepage_excerpt
    assert "UPDATE companies" in session.calls[0][0]
    insert_calls = [call for call in session.calls if "INSERT INTO contacts" in call[0]]
    assert len(insert_calls) == 1
    first_insert = insert_calls[0][1]
    assert first_insert["value"] == "hello@site.com"
    assert first_insert["is_primary"] is True
    status_calls = [call for call in session.calls if "SET status" in call[0]]
    assert status_calls
    assert status_calls[-1][1]["status"] == "contacts_ready"


@respx.mock
def test_enrich_company_uses_contact_page_text_email() -> None:
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    respx.get("https://site.com/").mock(
        return_value=httpx.Response(200, text="<html><body><h1>Главная</h1></body></html>")
    )
    respx.get("https://site.com/contact").mock(
        return_value=httpx.Response(
            200,
            text="""
            <html>
              <body>
                <p>Связаться можно по адресу office@site.com</p>
              </body>
            </html>
            """,
        )
    )

    inserted = enricher.enrich_company("company-4", "site.com", session=session)

    assert inserted == ["contact-1"]
    insert_calls = [call for call in session.calls if "INSERT INTO contacts" in call[0]]
    assert len(insert_calls) == 1
    assert insert_calls[0][1]["value"] == "office@site.com"


@respx.mock
def test_enrich_company_marks_not_found() -> None:
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    respx.get("https://empty.com/").mock(
        return_value=httpx.Response(
            200,
            text="""
            <html>
              <body>
                <h1>О компании</h1>
                <p>Без явных контактных email.</p>
              </body>
            </html>
            """,
            )
        )

    for suffix in ["contact", "contacts", "contacts/", "contact-us", "about", "about-us", "kontakty", "rekvizity", "company"]:
        respx.get(f"https://empty.com/{suffix}").mock(return_value=httpx.Response(404, text="not found"))

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
    assert isinstance(clients[0][0], httpx.Client)
    assert len(enricher.proxy_urls) == 3


@respx.mock
def test_enrich_company_retries_next_proxy_on_error(monkeypatch) -> None:
    monkeypatch.setenv("ENRICH_PROXY_URL", "http://proxy1.local:8080,http://proxy2.local:8080")
    session = DummySession()
    enricher = ContactEnricher(session_factory=lambda: session)  # type: ignore[arg-type]

    # Первый путь через direct вернёт 503 и будет пропущен, второй через proxy отдаст email.
    respx.get("https://example.com/").mock(return_value=httpx.Response(503, text="busy"))
    respx.get("https://example.com/contact").mock(
        return_value=httpx.Response(200, text="<html><body>info@example.com</body></html>")
    )

    inserted = enricher.enrich_company("company-5", "example.com", session=session)

    assert inserted == ["contact-1"]
    assert any("INSERT INTO contacts" in call[0] for call in session.calls)
