import os
from decimal import Decimal, InvalidOperation
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


def _to_decimal(value):
    if value is None or value == '':
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


class EncryptedDecimalField(models.DecimalField):
    """Confidential money value: behaves like a DecimalField (you read/write a
    Decimal, DRF/admin serialize it normally) but the DB column stores Fernet
    ciphertext instead of the amount. No key set -> plaintext passthrough.
    Reads tolerate ciphertext OR legacy plaintext (safe, resumable migration).
    Stored as text, so NOT usable in SQL Sum/filter/order — aggregate in Python.
    """

    def get_internal_type(self):
        return 'CharField'

    def db_type(self, connection):
        return 'varchar(255)'

    def from_db_value(self, value, expression, connection):
        if value in (None, ''):
            return None
        f = get_fernet()
        if f is not None:
            from cryptography.fernet import InvalidToken
            try:
                return Decimal(f.decrypt(value.encode()).decode())
            except (InvalidToken, Exception):
                pass  # legacy plaintext (pre-encryption)
        return _to_decimal(value)

    def to_python(self, value):
        if value is None or isinstance(value, Decimal):
            return value
        f = get_fernet()
        if f is not None and isinstance(value, str):
            from cryptography.fernet import InvalidToken
            try:
                return Decimal(f.decrypt(value.encode()).decode())
            except (InvalidToken, Exception):
                pass
        return _to_decimal(value)

    def get_prep_value(self, value):
        d = _to_decimal(value)
        if d is None:
            return None
        s = str(d)
        f = get_fernet()
        return f.encrypt(s.encode()).decode() if f is not None else s

    def get_db_prep_save(self, value, connection):
        # Bypass DecimalField numeric adaptation — store ciphertext text.
        return self.get_prep_value(value)
