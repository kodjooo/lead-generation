"""Тесты обогащения контактами."""

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
        self.counter += 1
        return DummyResult(f"contact-{self.counter}")

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
    phones = [c for c in contacts if c.contact_type == "phone"]

    assert len(emails) == 2
    assert len(phones) == 2
    assert {c.value.lower() for c in emails} == {"sales@example.com", "info@example.com"}


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
                <a href=\"mailto:hello@site.com\">Напишите нам</a>
                <p>Телефон: +1 202 555 0199</p>
              </body>
            </html>
            """,
        )
    )

    inserted = enricher.enrich_company("company-1", "https://site.com", session=session)

    assert inserted == ["contact-1", "contact-2"]
    assert len(session.calls) == 2
    first_call_sql, first_params = session.calls[0]
    assert "INSERT INTO contacts" in first_call_sql
    assert first_params["value"].lower() == "hello@site.com"
