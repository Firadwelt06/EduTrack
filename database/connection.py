import os

import mysql.connector


def get_connection(username=None, password=None):
    """Open a MySQL connection using application or migration credentials."""
    username = username or os.getenv("DB_USERNAME")
    password = password if password is not None else os.getenv("DB_PASSWORD")
    required = {
        "DB_HOST": os.getenv("DB_HOST"),
        "DB_DATABASE": os.getenv("DB_DATABASE"),
        "DB_USERNAME": username,
        "DB_PASSWORD": password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing database configuration: " + ", ".join(missing)
        )

    return mysql.connector.connect(
        host=required["DB_HOST"],
        user=required["DB_USERNAME"],
        password=required["DB_PASSWORD"],
        database=required["DB_DATABASE"],
        auth_plugin="mysql_native_password",
    )


def get_migration_connection():
    username = os.getenv("DB_MIGRATION_USERNAME")
    password = os.getenv("DB_MIGRATION_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "Set DB_MIGRATION_USERNAME and DB_MIGRATION_PASSWORD to a "
            "dedicated MySQL account with schema-migration privileges."
        )
    return get_connection(username=username, password=password)
