"""Тесты оркестратора пайплайна."""

from __future__ import annotations

from app.orchestrator import SELECT_CONTACTS_FOR_OUTREACH_SQL


def test_outreach_selection_excludes_failed_contacts() -> None:
    normalized_sql = " ".join(SELECT_CONTACTS_FOR_OUTREACH_SQL.split())
    assert "om.status IN ('sent', 'scheduled', 'failed')" in normalized_sql
    assert "ct.is_primary = TRUE" in normalized_sql
    assert "contacts_processing" in normalized_sql
