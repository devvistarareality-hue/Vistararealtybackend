"""Supabase Storage helpers for full-database backups.

Deliberately separate from supabase_storage.py (used for LOI documents) —
a full DB dump is far more sensitive than a single client's signed LOI, so it
always goes to its own PRIVATE bucket, never the public-URL path.
"""
import os
from urllib.parse import quote

import requests

BACKUP_BUCKET = 'backups'


def _base_and_key():
    return os.getenv('SUPABASE_URL', '').rstrip('/'), os.getenv('SUPABASE_SERVICE_KEY', '')


def ensure_backup_bucket():
    """Idempotently create the private 'backups' bucket. No-op if it already exists
    or Supabase isn't configured (local dev without env vars set)."""
    base, key = _base_and_key()
    if not (base and key):
        return
    try:
        requests.post(
            f'{base}/storage/v1/bucket',
            json={'id': BACKUP_BUCKET, 'name': BACKUP_BUCKET, 'public': False},
            headers={'Authorization': f'Bearer {key}', 'apikey': key, 'Content-Type': 'application/json'},
            timeout=20,
        )
    except Exception:
        pass  # a failed create-attempt (e.g. already exists) surfaces on the actual upload instead


def upload_backup(data, name):
    """Upload backup bytes to the private bucket using the service-role key.
    Returns the object path (== name) on success. Raises on failure."""
    base, key = _base_and_key()
    if not (base and key):
        raise Exception('Supabase is not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY missing).')
    r = requests.post(
        f'{base}/storage/v1/object/{BACKUP_BUCKET}/{quote(name)}',
        data=data,
        headers={'Authorization': f'Bearer {key}', 'apikey': key,
                 'Content-Type': 'application/gzip', 'x-upsert': 'true'},
        timeout=120,
    )
    if r.status_code not in (200, 201):
        raise Exception(f'Backup upload failed ({r.status_code}): {r.text[:200]}')
    return name


def signed_backup_url(name, expires_in=300):
    """Short-lived signed URL for a backup file. Returns None if Supabase isn't
    configured or signing failed."""
    base, key = _base_and_key()
    if not (base and key and name):
        return None
    try:
        r = requests.post(
            f'{base}/storage/v1/object/sign/{BACKUP_BUCKET}/{quote(name)}',
            json={'expiresIn': int(expires_in)},
            headers={'Authorization': f'Bearer {key}', 'apikey': key, 'Content-Type': 'application/json'},
            timeout=10,
        )
        if r.status_code == 200:
            signed = r.json().get('signedURL') or r.json().get('signedUrl')
            if signed:
                return f'{base}/storage/v1{signed}'
    except Exception:
        pass
    return None
