"""Django storage backend that stores files in Supabase Storage via its REST API.

Activated by setting DEFAULT_FILE_STORAGE to 'sales.supabase_storage.SupabaseStorage'.
Needs env vars: SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_BUCKET (default 'loi').
Uses the service_role key (server-side only) — bypasses RLS, so no policies needed.
"""
import os
import mimetypes
from urllib.parse import quote

import requests
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible


@deconstructible
class SupabaseStorage(Storage):
    def __init__(self):
        self.base   = os.getenv('SUPABASE_URL', '').rstrip('/')
        self.key    = os.getenv('SUPABASE_SERVICE_KEY', '')
        self.bucket = os.getenv('SUPABASE_BUCKET', 'loi')

    def _headers(self, extra=None):
        h = {'Authorization': f'Bearer {self.key}', 'apikey': self.key}
        if extra:
            h.update(extra)
        return h

    def _save(self, name, content):
        content.seek(0)
        data = content.read()
        ctype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        url = f'{self.base}/storage/v1/object/{self.bucket}/{quote(name)}'
        r = requests.post(url, data=data, headers=self._headers({'Content-Type': ctype, 'x-upsert': 'true'}), timeout=30)
        if r.status_code not in (200, 201):
            raise Exception(f'Supabase upload failed ({r.status_code}): {r.text[:200]}')
        return name

    def exists(self, name):
        # Names are made unique in get_available_name, so never block on existence.
        return False

    def get_available_name(self, name, max_length=None):
        # Keep the exact GAS-style path (project/plot/Rn). Re-uploads upsert.
        return name

    def url(self, name):
        return f'{self.base}/storage/v1/object/public/{self.bucket}/{quote(name)}'

    def delete(self, name):
        url = f'{self.base}/storage/v1/object/{self.bucket}/{quote(name)}'
        try:
            requests.delete(url, headers=self._headers(), timeout=15)
        except Exception:
            pass

    def size(self, name):
        return 0


def create_signed_url(name, expires_in=120):
    """Short-lived signed URL for a private-bucket object. Returned only to
    authenticated, authorised users — so confidential LOIs aren't publicly reachable.
    Returns None if Supabase isn't configured (local dev uses FileSystem instead)."""
    base   = os.getenv('SUPABASE_URL', '').rstrip('/')
    key    = os.getenv('SUPABASE_SERVICE_KEY', '')
    bucket = os.getenv('SUPABASE_BUCKET', 'loi')
    if not (base and key and name):
        return None
    try:
        r = requests.post(
            f'{base}/storage/v1/object/sign/{bucket}/{quote(name)}',
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
