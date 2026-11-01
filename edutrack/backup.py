import os
import subprocess
import getpass
import tempfile
import secrets
import keyring
from datetime import datetime

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives import hmac as crypto_hmac, hashes

KEYRING_SERVICE = "edutrack_backup"
KEYRING_USERNAME = "backup_encryption_key"


def _derive_keys(passphrase: bytes, salt: bytes):
    """Derive a 32-byte AES key and a separate 32-byte HMAC key from the
    passphrase + salt using scrypt. Two distinct derived keys (not the same
    bytes reused for both) so a weakness in one primitive can't cascade
    into the other -- standard encrypt-then-MAC hygiene."""
    kdf = Scrypt(salt=salt, length=64, n=2**14, r=8, p=1)
    derived = kdf.derive(passphrase)
    return derived[:32], derived[32:]

# Backup configuration
MYSQLDUMP_PATH = r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe"
BACKUP_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

def create_secure_option_file():
    """
    Writes a MySQL option file containing the DB password for mysqldump
    to read via --defaults-extra-file, then locks its permissions down
    via icacls so only the account running this process can read it.
    (os.chmod is a no-op for real access control on Windows — it only
    toggles the read-only attribute, not NTFS ACLs.)
    Caller is responsible for deleting the returned path.
    """
    fd, path = tempfile.mkstemp(prefix="mysqldump_opts_", suffix=".cnf", dir=BACKUP_FOLDER)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("[client]\n")
            f.write(f"password={os.getenv('DB_PASSWORD')}\n")

        # /inheritance:r strips whatever permissions this file would
        # otherwise inherit from the backups/ folder (e.g. a "Users"
        # group with read access). /grant:r replaces the ACL entirely
        # with just this one entry.
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
            check=True, capture_output=True
        )
    except Exception:
        os.remove(path)
        raise
    return path

def run_daily_backup():
    today_str = datetime.now().strftime("%Y-%m-%d")
    backup_filename = f"school_db_backup_{today_str}.sql.enc"
    backup_path = os.path.join(BACKUP_FOLDER, backup_filename)

    if os.path.exists(backup_path):
        print(f"Backup already exists for today: {backup_filename}")
        return

    os.makedirs(BACKUP_FOLDER, exist_ok=True)

    passphrase = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if not passphrase:
        print("ERROR: no backup passphrase in Windows Credential Manager. "
              "Run setup_backup_encryption.py once, then retry.")
        return
    passphrase = passphrase.encode("utf-8")

    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)  # CTR initial counter block
    aes_key, hmac_key = _derive_keys(passphrase, salt)

    encryptor = Cipher(algorithms.AES(aes_key), modes.CTR(nonce)).encryptor()
    mac = crypto_hmac.HMAC(hmac_key, hashes.SHA256())

    proc = None
    option_file = None
    try:
        option_file = create_secure_option_file()
        with open(backup_path, "wb") as out:
            # Header: salt + nonce. Not secret on their own -- only the
            # passphrase is -- but needed by the restore script to
            # re-derive the same keys.
            out.write(salt)
            out.write(nonce)

            proc = subprocess.Popen([
                MYSQLDUMP_PATH,
                f"--defaults-extra-file={option_file}",
                "--no-tablespaces",
                "--single-transaction",
                "-h", os.getenv("DB_HOST"),
                "-u", os.getenv("DB_USERNAME"),
                os.getenv("DB_DATABASE")
            ], stdout=subprocess.PIPE)

            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                ct_chunk = encryptor.update(chunk)
                mac.update(ct_chunk)
                out.write(ct_chunk)

            proc.stdout.close()
            proc.wait()
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, MYSQLDUMP_PATH)

            out.write(encryptor.finalize())
            out.write(mac.finalize())  # 32-byte tag appended at the end

        print(f"Encrypted backup created successfully: {backup_filename}")
    except subprocess.CalledProcessError as e:
        if os.path.exists(backup_path):
            os.remove(backup_path)  # don't leave a partial/corrupt backup behind
        print(f"Backup failed: {e}")
    finally:
        if proc and proc.stdout and not proc.stdout.closed:
            proc.stdout.close()
        if option_file and os.path.exists(option_file):
            os.remove(option_file)

def cleanup_old_backups(days_to_keep=14):
    if not os.path.exists(BACKUP_FOLDER):
        return

    now = datetime.now()
    deleted_count = 0

    for filename in os.listdir(BACKUP_FOLDER):
        if filename.startswith("school_db_backup_") and filename.endswith(".sql.enc"):
            file_path = os.path.join(BACKUP_FOLDER, filename)
            file_age_days = (now - datetime.fromtimestamp(os.path.getmtime(file_path))).days

            if file_age_days > days_to_keep:
                os.remove(file_path)
                deleted_count += 1

    if deleted_count > 0:
        print(f"Deleted {deleted_count} backup(s) older than {days_to_keep} days.")
