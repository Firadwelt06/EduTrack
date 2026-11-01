import hashlib
import json
import logging
import secrets
import urllib.error
import urllib.request
from functools import wraps

from flask import current_app, flash, has_app_context, redirect, url_for
from flask_login import current_user


def _logger():
    return current_app.logger if has_app_context() else logging.getLogger("edutrack")


def roles_required(*allowed_roles):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if current_user.role not in allowed_roles:
                flash("You don't have permission to access that page.", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return wrapped
    return decorator

#Shared validation helper
PASSWORD_MIN_LENGTH = 12

def validate_new_password(password):
    """Returns None if the password is acceptable, or an error string if not.
    Checks length per NIST 800-63B guidance (length over composition rules),
    then checks the password against the HaveIBeenPwned Pwned Passwords API
    using the k-anonymity model: only the first 5 characters of the SHA-1
    hash are ever sent, so the real password (and even its full hash) never
    leaves this machine. If the API is unreachable, we skip that check
    rather than block the user from changing their password over a
    third-party outage."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."

    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]

    try:
        req = urllib.request.Request(
            f"https://api.pwnedpasswords.com/range/{prefix}",
            headers={"User-Agent": "EduTrack-App"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8")
        for line in body.splitlines():
            hash_suffix, count = line.split(":")
            if hash_suffix == suffix:
                return ("This password has appeared in a known data breach. "
                        "Please choose a different one.")
    except (urllib.error.URLError, TimeoutError):
        _logger().warning("Pwned Passwords check skipped (API unreachable).")

    return None

# Rate limiting helper: exponential backoff for repeated failures
def backoff_seconds(failed_attempts):
    """Exponential delay after repeated failures, capped at 5 minutes.
    No delay for the first couple of failures — normal typos shouldn't
    be punished, only sustained guessing."""
    if failed_attempts < 3:
        return 0
    return min(300, 2 ** (failed_attempts - 2))  # 2s, 4s, 8s ... capped

# Audit logging helper
def log_audit(cursor, action, entity_type, entity_id=None, details=None):
    """Record an audit trail entry. Call this AFTER the real change has
    succeeded (same cursor/transaction, before conn.commit()) so the audit
    row and the actual change commit or roll back together — you never
    want an audit log claiming something happened that didn't actually
    stick, or a real change with no record of who made it."""
    cursor.execute(
        """INSERT INTO audit_log (user_id, username, action, entity_type, entity_id, details)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (current_user.id, current_user.username, action, entity_type, entity_id,
         json.dumps(details) if details is not None else None)
    )

# Correlation ref logging for database errors. This is a security measure to avoid exposing raw DB errors to users, while still allowing developers to trace issues.
def log_db_error(exc):
    """Log the real exception server-side under a short correlation ref,
    and return that ref so the caller can show it to the user instead of
    the raw exception text."""
    ref = secrets.token_hex(4)
    _logger().error("DB error [ref=%s]: %s", ref, exc, exc_info=True)
    return ref
