import argparse

from dotenv import load_dotenv

from database.connection import get_migration_connection
from database.migrations.runner import current_versions, upgrade


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="EduTrack maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    database_parser = subparsers.add_parser("db", help="Database commands")
    database_commands = database_parser.add_subparsers(
        dest="database_command", required=True
    )
    database_commands.add_parser("upgrade", help="Apply pending schema migrations")
    database_commands.add_parser("current", help="List applied schema migrations")
    args = parser.parse_args()

    connection = get_migration_connection()
    try:
        if args.database_command == "upgrade":
            applied = upgrade(connection)
            if applied:
                print("Applied migrations: " + ", ".join(applied))
            else:
                print("Database schema is already up to date.")
        else:
            versions = current_versions(connection)
            if versions:
                print("Applied migrations: " + ", ".join(versions))
            else:
                print("No EduTrack migrations have been applied.")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
