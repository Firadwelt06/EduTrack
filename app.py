import csv
import io
import os
import subprocess
import urllib.error
import urllib.request

import mysql.connector

from edutrack import backup, domain, routes
from edutrack.backup import (
    BACKUP_FOLDER,
    KEYRING_SERVICE,
    KEYRING_USERNAME,
    MYSQLDUMP_PATH,
    _derive_keys,
    cleanup_old_backups,
    create_secure_option_file,
    run_daily_backup,
)
from edutrack.domain import (
    PASSWORD_MIN_LENGTH,
    backoff_seconds,
    log_audit,
    log_db_error,
    roles_required,
    validate_new_password,
)
from edutrack.extensions import csrf, limiter, login_manager
from edutrack.factory import create_app
from edutrack.routes import User, get_conn, sanitize_csv_field


app = create_app()


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    run_daily_backup()
    cleanup_old_backups()
    app.run(debug=debug_mode)
