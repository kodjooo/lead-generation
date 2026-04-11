"""Тесты bootstrap и миграций базы данных."""

from app.modules.utils import db


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class FakeConnection:
    def __init__(self, engine):
        self.engine = engine
        self.lock_calls = []

    def execute(self, statement, params=None):
        query = str(statement)
        if "pg_advisory_lock" in query:
            self.lock_calls.append(("lock", params["lock_id"]))
        elif "pg_advisory_unlock" in query:
            self.lock_calls.append(("unlock", params["lock_id"]))
        return FakeResult(1)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeEngine:
    def __init__(self):
        self.connection = FakeConnection(self)

    def connect(self):
        return self.connection


def test_bootstrap_database_uses_advisory_lock(monkeypatch) -> None:
    engine = FakeEngine()
    observed = {}

    def fake_run_sql_migrations(engine=None, migrations_path=None):
        observed["engine"] = engine
        observed["migrations_path"] = migrations_path
        return ["0001_init.sql"]

    monkeypatch.setattr(db, "run_sql_migrations", fake_run_sql_migrations)

    applied = db.bootstrap_database(engine=engine)

    assert applied == ["0001_init.sql"]
    assert observed["engine"] is engine
    assert observed["migrations_path"] is None
    assert engine.connection.lock_calls == [
        ("lock", db.MIGRATIONS_ADVISORY_LOCK_ID),
        ("unlock", db.MIGRATIONS_ADVISORY_LOCK_ID),
    ]
