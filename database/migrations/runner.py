import importlib.util
import re
from pathlib import Path


MIGRATION_TABLE = "schema_migrations"
MIGRATION_PATTERN = re.compile(r"v(\d{4})_[a-z0-9_]+\.py$")
MIGRATIONS_DIR = Path(__file__).parent


def _migration_files(migrations_dir=MIGRATIONS_DIR):
    migrations = []
    versions = set()
    for path in sorted(migrations_dir.glob("v*.py")):
        match = MIGRATION_PATTERN.fullmatch(path.name)
        if not match:
            raise RuntimeError(f"Invalid migration filename: {path.name}")
        version = match.group(1)
        if version in versions:
            raise RuntimeError(f"Duplicate database migration version: {version}")
        versions.add(version)
        migrations.append((version, path))
    return migrations


def _load_migration(version, path):
    spec = importlib.util.spec_from_file_location(
        f"edutrack_migration_{version}", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load database migration: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    upgrade = getattr(module, "upgrade", None)
    if not callable(upgrade):
        raise RuntimeError(f"Migration {path.name} has no upgrade(cursor) function")
    return upgrade


def _ensure_core_schema(cursor):
    cursor.execute(
        """
        SELECT COUNT(*) AS table_count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users'
        """
    )
    if cursor.fetchone()["table_count"] == 0:
        raise RuntimeError(
            "The EduTrack database is not initialized. Import "
            "database/schema.sql for a new database before migrating."
        )


def _migration_table_exists(cursor):
    cursor.execute(
        """
        SELECT COUNT(*) AS table_count
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        """,
        (MIGRATION_TABLE,),
    )
    return cursor.fetchone()["table_count"] > 0


def current_versions(connection):
    cursor = connection.cursor(dictionary=True, buffered=True)
    try:
        if not _migration_table_exists(cursor):
            return []
        cursor.execute(f"SELECT version FROM `{MIGRATION_TABLE}` ORDER BY version")
        return [row["version"] for row in cursor.fetchall()]
    finally:
        cursor.close()


def upgrade(connection, migrations_dir=MIGRATIONS_DIR):
    cursor = connection.cursor(dictionary=True, buffered=True)
    try:
        _ensure_core_schema(cursor)
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{MIGRATION_TABLE}` (
              `version` varchar(4) NOT NULL,
              `applied_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (`version`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        connection.commit()

        cursor.execute(f"SELECT version FROM `{MIGRATION_TABLE}`")
        applied = {row["version"] for row in cursor.fetchall()}
        known_versions = {
            version for version, _ in _migration_files(migrations_dir)
        }
        unknown_versions = sorted(applied - known_versions)
        if unknown_versions:
            raise RuntimeError(
                "The database has migration(s) unknown to this code version: "
                + ", ".join(unknown_versions)
                + ". Update EduTrack before running migrations."
            )
        newly_applied = []

        for version, path in _migration_files(migrations_dir):
            if version in applied:
                continue
            migration = _load_migration(version, path)
            try:
                migration(cursor)
                cursor.execute(
                    f"INSERT INTO `{MIGRATION_TABLE}` (version) VALUES (%s)",
                    (version,),
                )
                connection.commit()
            except Exception as exc:
                connection.rollback()
                raise RuntimeError(
                    f"Database migration {version} ({path.name}) failed. "
                    "Fix the cause and rerun; migrations must be safe to retry."
                ) from exc
            newly_applied.append(version)

        return newly_applied
    finally:
        cursor.close()
