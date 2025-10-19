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

    assert inserted == ["contact-1", "contact-2", "contact-3", "contact-4"]
    # первый вызов — обновление companies с homepage_excerpt
    assert "UPDATE companies" in session.calls[0][0]
    insert_calls = [call for call in session.calls if "INSERT INTO contacts" in call[0]]
    assert len(insert_calls) == 4
    first_insert = insert_calls[0][1]
    assert first_insert["value"] == "hello@site.com"
    assert first_insert["is_primary"] is True
    second_insert = insert_calls[1][1]
    assert second_insert["value"] == "+79001234567"
    assert second_insert["is_primary"] is True
    third_insert = insert_calls[2][1]
    assert third_insert["value"] == "sales@site.com"
    assert third_insert["is_primary"] is False
    fourth_insert = insert_calls[3][1]
    assert fourth_insert["value"] == "88005553535"
    assert fourth_insert["is_primary"] is False
