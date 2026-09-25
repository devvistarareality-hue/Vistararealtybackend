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


XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

# What a stored backup is, by its extension. This used to be hard-coded to gzip,
# from when the only stored backup was a gzipped JSON dump. The scheduled backup
# is a workbook now, and labelling it gzip meant the browser saved it as
# "Company-20260925-1641.gz" — a perfectly good .xlsx that Excel would not open
# and the Restore panel would not accept, because of a header.
CONTENT_TYPES = {'.xlsx': XLSX_TYPE, '.gz': 'application/gzip', '.json': 'application/json'}


def content_type_for(name):
    for ext, ctype in CONTENT_TYPES.items():
        if name.lower().endswith(ext):
            return ctype
    return 'application/octet-stream'


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
                 'Content-Type': content_type_for(name), 'x-upsert': 'true'},
        timeout=120,
    )
    if r.status_code not in (200, 201):
        raise Exception(f'Backup upload failed ({r.status_code}): {r.text[:200]}')
    return name


def signed_backup_url(name, expires_in=300, download_as=None):
    """Short-lived signed URL for a backup file. Returns None if Supabase isn't
    configured or signing failed.

    `download_as` adds Supabase's ?download= parameter, which sets
    Content-Disposition with that filename. That is what decides the name on
    disk, so it also rescues files already in the bucket under the old gzip
    content type — the bytes were always a workbook, only the label was wrong.
    """
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
                url = f'{base}/storage/v1{signed}'
                if download_as:
                    url += ('&' if '?' in url else '?') + 'download=' + quote(download_as)
                return url
    except Exception:
        pass
    return None
