from pathlib import Path

import pytest

from database import connection as db_connection
from database.migrations import runner


class MigrationCursor:
    def __init__(self, tables, columns, applied):
        self.tables = tables
        self.columns = columns
        self.applied = applied
        self.result = []
        self.closed = False
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if "information_schema.TABLES" in query:
            if "TABLE_NAME = 'users'" in query:
                count = int("users" in self.tables)
            else:
                count = int((params or ("",))[0] in self.tables)
            self.result = [{"table_count": count}]
        elif "information_schema.COLUMNS" in query:
            column = query.rsplit("COLUMN_NAME = '", 1)[1].split("'", 1)[0]
            self.result = [{"column_count": int(column in self.columns)}]
        elif query.lstrip().startswith("CREATE TABLE"):
            table = query.split("`", 2)[1]
            self.tables.add(table)
        elif query.startswith("ALTER TABLE users ADD COLUMN"):
            column = query.split("ADD COLUMN", 1)[1].strip().split()[0]
            self.columns.add(column)
        elif query.startswith("SELECT version FROM `schema_migrations`"):
            self.result = [{"version": version} for version in sorted(self.applied)]
        elif query.startswith("INSERT INTO `schema_migrations`"):
            self.applied.add(params[0])
            self.result = []
        else:
            raise AssertionError(f"Unexpected migration query: {query}")

    def fetchone(self):
        return self.result.pop(0) if self.result else None

    def fetchall(self):
        result, self.result = self.result, []
        return result

    def close(self):
        self.closed = True


class MigrationConnection:
    def __init__(self, tables=("users",), columns=(), applied=()):
        self.tables = set(tables)
        self.columns = set(columns)
        self.applied = set(applied)
        self.fake_cursor = MigrationCursor(self.tables, self.columns, self.applied)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, **kwargs):
        return self.fake_cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_migration_adds_missing_schema_without_changing_existing_user_data():
    connection = MigrationConnection()
    user_rows = [{"user_id": 1, "username": "admin", "password_hash": "hash"}]

    applied = runner.upgrade(connection)

    assert applied == ["0001"]
    assert {"schema_migrations", "audit_log", "users"} <= connection.tables
    assert {"failed_login_attempts", "locked_until"} <= connection.columns
    assert connection.applied == {"0001"}
    assert user_rows == [{"user_id": 1, "username": "admin", "password_hash": "hash"}]
    assert connection.commits == 2
    assert connection.rollbacks == 0
    assert connection.fake_cursor.closed


def test_migration_runner_is_idempotent_after_first_upgrade():
    connection = MigrationConnection(
        tables=("users", "schema_migrations", "audit_log"),
        columns=("failed_login_attempts", "locked_until"),
        applied=("0001",),
    )

    assert runner.upgrade(connection) == []
    assert connection.applied == {"0001"}


def test_migration_runner_rejects_versions_newer_than_the_code():
    connection = MigrationConnection(
        tables=("users", "schema_migrations"),
        applied=("9999",),
    )

    with pytest.raises(RuntimeError, match="unknown to this code version"):
        runner.upgrade(connection)


def test_failed_migration_is_reported_and_rolled_back(tmp_path):
    (tmp_path / "v0001_ok.py").write_text(
        "def upgrade(cursor):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "v0002_fail.py").write_text(
        "def upgrade(cursor):\n"
        "    raise ValueError('migration failure')\n",
        encoding="utf-8",
    )
    connection = MigrationConnection(
        tables=("users", "schema_migrations"),
        applied=("0001",),
    )

    with pytest.raises(RuntimeError, match="v0002_fail.py"):
        runner.upgrade(connection, migrations_dir=tmp_path)

    assert connection.rollbacks == 1
    assert connection.applied == {"0001"}
    assert connection.fake_cursor.closed


def test_migration_refuses_uninitialized_database():
    connection = MigrationConnection(tables=())

    with pytest.raises(RuntimeError, match="not initialized"):
        runner.upgrade(connection)

    assert "schema_migrations" not in connection.tables
    assert connection.fake_cursor.closed


def test_migration_filenames_must_be_versioned(tmp_path):
    (tmp_path / "vbad.py").write_text("pass\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Invalid migration filename"):
        runner._migration_files(tmp_path)


def test_schema_bootstrap_is_idempotent_and_does_not_drop_existing_tables():
    schema_path = Path(__file__).parents[1] / "database" / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8").upper()

    assert "DROP TABLE" not in schema
    assert schema.count("CREATE TABLE IF NOT EXISTS") == 8
    assert "`FAILED_LOGIN_ATTEMPTS` INT NOT NULL DEFAULT '0'" in schema
    assert "`LOCKED_UNTIL` DATETIME DEFAULT NULL" in schema


def test_application_connection_uses_explicit_runtime_credentials(monkeypatch):
    captured = {}
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_DATABASE", "school_db")
    monkeypatch.setenv("DB_USERNAME", "school_app")
    monkeypatch.setenv("DB_PASSWORD", "runtime-password")
    monkeypatch.setattr(
        db_connection.mysql.connector,
        "connect",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    db_connection.get_connection()

    assert captured == {
        "host": "localhost",
        "user": "school_app",
        "password": "runtime-password",
        "database": "school_db",
        "auth_plugin": "mysql_native_password",
    }


def test_migration_connection_requires_separate_credentials(monkeypatch):
    monkeypatch.delenv("DB_MIGRATION_USERNAME", raising=False)
    monkeypatch.delenv("DB_MIGRATION_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="DB_MIGRATION_USERNAME"):
        db_connection.get_migration_connection()


def test_migration_connection_uses_separate_schema_credentials(monkeypatch):
    captured = {}
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_DATABASE", "school_db")
    monkeypatch.setenv("DB_USERNAME", "school_app")
    monkeypatch.setenv("DB_PASSWORD", "runtime-password")
    monkeypatch.setenv("DB_MIGRATION_USERNAME", "school_migrator")
    monkeypatch.setenv("DB_MIGRATION_PASSWORD", "migration-password")
    monkeypatch.setattr(
        db_connection.mysql.connector,
        "connect",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    db_connection.get_migration_connection()

    assert captured["user"] == "school_migrator"
    assert captured["password"] == "migration-password"
