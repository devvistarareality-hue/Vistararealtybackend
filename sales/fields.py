import os
from django.db import models


def get_fernet():
    """Return a Fernet instance from FIELD_ENCRYPTION_KEY, or None if unset.

    When None, EncryptedTextField behaves as a plain TextField (passthrough) so
    the app keeps working before the key is provisioned.
    """
    key = os.getenv('FIELD_ENCRYPTION_KEY', '').strip()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


class EncryptedTextField(models.TextField):
    """TextField that transparently encrypts its value at rest with Fernet.

    - Encrypts on write, decrypts on read — the Python attribute is always plaintext.
    - No key set  -> passthrough (plaintext), so nothing breaks pre-provisioning.
    - On read, a value that can't be decrypted (legacy plaintext / mid-migration)
      is returned as-is, so mixed plaintext/ciphertext states are handled.
    Note: equality lookups won't work (Fernet is non-deterministic) — only use on
    fields you never filter by exact value.
    """

    def from_db_value(self, value, expression, connection):
        if value in (None, ''):
            return value
        f = get_fernet()
        if f is None:
            return value
        from cryptography.fernet import InvalidToken
        try:
            return f.decrypt(value.encode()).decode()
        except (InvalidToken, Exception):
            return value  # not encrypted (plaintext / pre-migration)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ''):
            return value
        f = get_fernet()
        if f is None:
            return value
        from cryptography.fernet import InvalidToken
        try:
            f.decrypt(value.encode())
            return value  # already ciphertext — don't double-encrypt
        except (InvalidToken, Exception):
            pass
        return f.encrypt(value.encode()).decode()
