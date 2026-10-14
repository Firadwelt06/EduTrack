import os

import pytest

from database.connection import get_migration_connection
from database.migrations.runner import current_versions, upgrade


TEST_DATABASE = os.getenv("EDUTRACK_MYSQL_TEST_DATABASE")


@pytest.mark.skipif(
    not TEST_DATABASE,
    reason="Set EDUTRACK_MYSQL_TEST_DATABASE to an empty disposable MySQL schema.",
)
def test_existing_installation_upgrades_without_losing_user_data(monkeypatch):
    if not TEST_DATABASE.startswith("edutrack_test_"):
        pytest.fail(
            "EDUTRACK_MYSQL_TEST_DATABASE must start with 'edutrack_test_' "
            "to prevent changes to a non-test database."
        )

    monkeypatch.setenv("DB_DATABASE", TEST_DATABASE)
    connection = get_migration_connection()
    cursor = connection.cursor(dictionary=True, buffered=True)
    owns_schema_contents = False
    try:
        cursor.execute("SHOW TABLES")
        if cursor.fetchall():
            pytest.fail(
                "The configured migration test schema must be empty before "
                "the integration test starts."
            )

        owns_schema_contents = True
        cursor.execute(
            """
            CREATE TABLE users (
              user_id int NOT NULL AUTO_INCREMENT,
              username varchar(50) NOT NULL,
              password_hash varchar(255) NOT NULL,
              role varchar(20) NOT NULL,
              student_id int DEFAULT NULL,
              teacher_id int DEFAULT NULL,
              must_change_password tinyint(1) NOT NULL DEFAULT 0,
              PRIMARY KEY (user_id),
              UNIQUE KEY username (username)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            INSERT INTO users (username, password_hash, role)
            VALUES ('migration-sentinel', 'preserve-this-hash', 'admin')
            """
        )
        connection.commit()

        assert upgrade(connection) == ["0001"]
        assert current_versions(connection) == ["0001"]

        cursor.execute(
            """
            SELECT username, password_hash, failed_login_attempts, locked_until
            FROM users
            WHERE username = %s
            """,
            ("migration-sentinel",),
        )
        user = cursor.fetchone()
        assert user == {
            "username": "migration-sentinel",
            "password_hash": "preserve-this-hash",
            "failed_login_attempts": 0,
            "locked_until": None,
        }
        cursor.execute(
            """
            SELECT COUNT(*) AS table_count
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_log'
            """
        )
        assert cursor.fetchone()["table_count"] == 1
    finally:
        if owns_schema_contents:
            cursor.execute(
                "DROP TABLE IF EXISTS audit_log, schema_migrations, users"
            )
            connection.commit()
        cursor.close()
        connection.close()
