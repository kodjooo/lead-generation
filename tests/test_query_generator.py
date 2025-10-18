"""Тесты генератора запросов."""

from datetime import datetime, timezone

from app.modules.query_generator import NicheRow, QueryGenerator


def test_query_generator_builds_queries_with_triggers() -> None:
    generator = QueryGenerator(now_func=lambda: datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc))
    row = NicheRow(row_index=2, niche="стоматология", city="Москва", country="Россия", batch_tag="batch-1")

    queries = generator.generate(row)

    assert len(queries) == 6

    first_query = queries[0]
    assert first_query.metadata["trigger"] is None
    assert first_query.query_text.startswith("lang:ru стоматология Москва")
    assert first_query.region_code == 213
    assert first_query.scheduled_for == datetime(2025, 1, 1, 20, 0, tzinfo=timezone.utc)

    second_query = queries[1]
    assert '"оставить заявку"' in second_query.query_text
    assert second_query.metadata["trigger"] == '"оставить заявку"'


def test_query_generator_fallback_region() -> None:
    generator = QueryGenerator(now_func=lambda: datetime(2025, 1, 2, 3, 0, tzinfo=timezone.utc))
    row = NicheRow(row_index=3, niche="грузоперевозки", city="Неизвестный город", country="Казахстан", batch_tag=None)

    queries = generator.generate(row)

    assert queries
    assert queries[0].region_code == 225  # fallback
    # так как вызываем ночью, расписание начинается немедленно
    assert queries[0].scheduled_for == datetime(2025, 1, 2, 3, 0, tzinfo=timezone.utc)
