# EduTrack

A Flask + MySQL school database management system for tracking students,
courses, enrollments, and grades across academic years and semesters.
Built as a hands-on security/DBA learning project — the goal is a genuinely
production-conscious app, not just a portfolio demo.

## Features

- **Role-based access control** (admin / teacher / student-parent), enforced
  at both the route level and the data level — e.g. a teacher can only view
  or grade students in courses they actually teach, not just any student ID.
- **Passwords hashed with werkzeug's salted scrypt** (not raw SHA256), with a
  forced-password-reset flow for migrated/seeded accounts.
- **Password policy per NIST 800-63B guidance**: minimum length (12 chars)
  over composition rules, plus a best-effort check against the
  HaveIBeenPwned Pwned Passwords API using the k-anonymity model — only the
  first 5 characters of the password's SHA-1 hash are ever sent, so the real
  password never leaves the machine. The check is skipped (not blocking) if
  the API is unreachable.
- **CSRF protection** (Flask-WTF) on every state-changing form.
- **Rate limiting and account-level backoff on `/login`**: Flask-Limiter caps
  attempts per IP, and a per-account exponential backoff (stored on the
  `users` table) throttles repeated failures against one specific account —
  without a hard lockout, so an attacker can't weaponize the throttle to
  lock a legitimate user out on purpose.
- **Session cookies configured explicitly**: `HttpOnly` (blocks JS access),
  `SameSite=Lax` (defense-in-depth against CSRF alongside the token check),
  and `Secure` toggled automatically based on environment (off for local
  HTTP dev, on everywhere else).
- **Debug mode gated behind an environment variable**, defaulting to off —
  Flask's interactive debugger (which grants a Python shell on unhandled
  exceptions) can never accidentally ship enabled.
- **Server-side error logging with correlation IDs**: database exceptions
  are logged in full (with traceback) to a rotating log file, and users see
  a generic message with a short reference code instead of raw exception
  text — closing an information-disclosure gap without losing debuggability.
- **Audit trail** for grade changes, enrollments, and deletions (academic
  years, semesters, teachers, courses): who did it, what changed (old/new
  values where relevant), and when — viewable by admins at `/audit-log`,
  filterable by entity type.
- **CSV student import** hardened against more than just a bad file
  extension: content is sanity-checked against expected headers before
  parsing, fields are sanitized against spreadsheet formula injection
  (e.g. `=HYPERLINK(...)`), and request size is capped to prevent memory
  exhaustion from an oversized upload.
- **Encrypted-at-rest daily backups**: `mysqldump` output is streamed
  straight into AES-256-CTR encryption with an HMAC-SHA256 integrity tag
  (encrypt-then-MAC) — plaintext SQL never touches disk. The encryption
  passphrase is stored in Windows Credential Manager (via `keyring`), not
  in a config file. A scheduled integrity-check script verifies the latest
  backup decrypts and its HMAC matches, without ever writing plaintext to
  disk, catching a broken passphrase or corrupted backup before it's
  actually needed.

## Tech Stack

- Python 3 / Flask
- MySQL (developed against MySQL Server 8.0)
- Flask-Login, Flask-WTF, Flask-Limiter, werkzeug
- `cryptography` + `keyring` for backup encryption

## Setup

### 1. Clone and create a virtual environment

```powershell
git clone <your-repo-url>
cd EduTrack
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in real values:

```
DB_USERNAME=your_username_here
DB_PASSWORD=your_password_here
DB_HOST=localhost
DB_DATABASE=school_db
SECRET_KEY=generate-a-long-random-value-here
FLASK_DEBUG=false
```

`SECRET_KEY` should be a long random string (used by Flask for session
signing) — don't reuse the placeholder from `.env.example`.

`FLASK_DEBUG` controls two things at once: Flask's interactive debugger, and
whether session cookies get the `Secure` flag. Only set it to `true` for
local development over plain HTTP — leave it unset (or `false`) anywhere
else, since debug mode's interactive shell on unhandled exceptions is a
remote-code-execution risk if it's ever reachable by anyone but you.

### 3. Create the database and import the schema

```sql
CREATE DATABASE school_db;
```

```powershell
mysql -u your_username -p school_db < database\schema.sql
```

### 4. Create the first admin account

The app has no public registration route by design — accounts are
provisioned directly. Generate a proper password hash:

```powershell
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('YourStrongPasswordHere'))"
```

Then insert the account:

```sql
INSERT INTO users (username, password_hash, role, must_change_password)
VALUES ('admin', 'scrypt:...paste-hash-here...', 'admin', 0);
```

Passwords must be at least 12 characters (see Features above) — the
HaveIBeenPwned check only runs through the app's own change-password flow,
not this manual insert, so pick something that isn't a known breached
password anyway.

### 5. (Optional) Enable encrypted backups

Requires `mysqldump.exe` — update the `MYSQLDUMP_PATH` constant in `app.py`
if your MySQL install location differs from the default. Then seed the
backup encryption passphrase once per machine:

```powershell
python setup_backup_encryption.py
```

This stores the passphrase in Windows Credential Manager. Backups run
automatically on app startup (`run_daily_backup()`), skipping if a backup
for the current day already exists.

**Off-disk storage:** `BACKUP_FOLDER` should point somewhere that isn't
just the same disk as your live database — e.g. a path inside a synced
OneDrive/Google Drive folder, or a location mirrored to external/cloud
storage via a separate scheduled sync. Because backups are encrypted
client-side before they're written, syncing the encrypted file to a cloud
folder doesn't expose plaintext data to that provider.

To restore/verify a backup:

```powershell
python decrypt_backup.py backups\school_db_backup_2026-07-10.sql.enc restored.sql
```

**Scheduled integrity checks:** `verify_latest_backup.py` decrypts the most
recent backup in-memory (never writing plaintext to disk) and confirms its
HMAC tag is valid — catching a broken passphrase, corrupted file, or
pipeline bug automatically. Schedule it separately from backup creation
(e.g. weekly via Windows Task Scheduler):

```powershell
python verify_latest_backup.py
```

This proves a backup *decrypts cleanly*, not that its SQL is complete or
well-formed — pair it with an occasional full manual restore into a scratch
database, done by a human, on whatever cadence you're comfortable with
(monthly is a reasonable starting point).

### 6. Run the app

```powershell
python app.py
```

## Project Structure

```
EduTrack/
├── app.py                       # Main Flask application
├── migrate_password_hashing.py  # One-time SHA256 -> scrypt migration
├── setup_backup_encryption.py   # One-time backup passphrase setup
├── decrypt_backup.py            # Backup restore/verification tool (writes plaintext)
├── verify_latest_backup.py      # Scheduled integrity check (in-memory only)
├── database/
│   └── schema.sql               # Table structure only, no data
├── templates/                   # Jinja2 templates
├── backups/                     # Encrypted daily backups (gitignored)
├── logs/                        # Rotating application logs (gitignored)
├── .env.example                 # Template for required environment variables
└── requirements.txt
```

## Known Limitations / In Progress

This project is being built and audited incrementally. Currently open items:

- Backup restore-testing currently relies on the automated integrity check
  plus a manually-scheduled full restore test — there's no fully automated
  "restore into a scratch DB and validate row counts" step yet.
- No CAPTCHA or similar friction on `/login` beyond IP rate limiting and
  per-account backoff — acceptable at current scale, worth revisiting if
  this is ever exposed to a wider audience.
- Audit log currently covers grades, enrollments, and deletions
  (years/semesters/teachers/courses) — student edits (`edit_student`) and
  student creation/deletion are not yet audited.
- Flask-Limiter's default storage is in-process memory, which resets on
  restart and wouldn't work correctly across multiple worker processes —
  fine for the current single-process deployment, but would need a shared
  backend (e.g. Redis) if that ever changes.

This list is intentionally public — part of the point of this project is
practicing security auditing in the open rather than presenting a polished
facade.

## AI Assistance Disclosure

In building this project, I collaborated with Claude (Anthropic) for security
auditing, code review, and implementation guidance — including the
authentication hardening, encryption design, rate limiting, audit logging,
and input-validation fixes documented above. I affirm that all AI-assisted
content underwent thorough review on my part: I evaluated each suggestion,
understood the reasoning behind it, and the final implementation reflects my
own understanding of the trade-offs involved. While AI assistance was
instrumental to the process, I maintain full responsibility for the code,
its correctness, and its security posture. This disclosure is made in the
interest of transparency about how the project was built.

## License

Apache License 2.0 — see `LICENSE`.