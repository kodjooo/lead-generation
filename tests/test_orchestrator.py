"""Тесты оркестратора пайплайна."""

from __future__ import annotations

from app.orchestrator import SELECT_CONTACTS_FOR_OUTREACH_SQL


def test_outreach_selection_excludes_failed_contacts() -> None:
    normalized_sql = " ".join(SELECT_CONTACTS_FOR_OUTREACH_SQL.split())
    assert "om.status IN ('sent', 'scheduled', 'failed')" in normalized_sql
    assert "ct.is_primary = TRUE" in normalized_sql
    assert "contacts_processing" in normalized_sql


def test_company_backfill_runs_only_once() -> None:
    normalized_sql = " ".join(SELECT_COMPANIES_WITHOUT_CONTACTS_SQL.split())
    assert "c.status = 'new'" in normalized_sql
    assert "c.status = 'contacts_not_found'" in normalized_sql
    assert "contacts_backfill_done_at" in normalized_sql
