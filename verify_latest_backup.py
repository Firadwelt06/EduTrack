"""
Scheduled integrity check: decrypts the most recent backup in-memory
(never writing plaintext to disk) and verifies its HMAC tag. This catches
the most likely failure mode -- a broken passphrase, corrupted file, or
encryption pipeline bug -- without needing a human to inspect SQL output.

Run this on a schedule (e.g. Windows Task Scheduler, weekly) separate from
run_daily_backup() itself, so a bug in backup creation and a bug in backup
verification aren't accidentally the same blind spot.

This does NOT replace a periodic full manual restore test -- add that to
your calendar too. This only proves the file decrypts and its integrity
tag matches; it doesn't prove the SQL inside is well-formed or complete.
"""
import glob
import os
import sys
import keyring
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import hmac as crypto_hmac, hashes
from cryptography.exceptions import InvalidSignature
from app import _derive_keys, KEYRING_SERVICE, KEYRING_USERNAME, BACKUP_FOLDER


def verify_latest():
    pattern = os.path.join(BACKUP_FOLDER, "school_db_backup_*.sql.enc")
    backups = sorted(glob.glob(pattern))
    if not backups:
        print("VERIFY FAILED: no backup files found.")
        sys.exit(1)

    latest = backups[-1]
    print(f"Verifying: {os.path.basename(latest)}")

    passphrase = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    if not passphrase:
        print("VERIFY FAILED: no passphrase in Credential Manager.")
        sys.exit(1)
    passphrase = passphrase.encode("utf-8")

    with open(latest, "rb") as f:
        salt = f.read(16)
        nonce = f.read(16)
        body = f.read()
    ciphertext, tag = body[:-32], body[-32:]

    aes_key, hmac_key = _derive_keys(passphrase, salt)
    mac = crypto_hmac.HMAC(hmac_key, hashes.SHA256())
    mac.update(ciphertext)
    try:
        mac.verify(tag)
    except InvalidSignature:
        print("VERIFY FAILED: HMAC mismatch -- backup is corrupted or passphrase changed.")
        sys.exit(1)

    print("VERIFY OK: integrity tag matches. Backup is decryptable and intact.")
    sys.exit(0)


if __name__ == "__main__":
    verify_latest()