import os
import requests
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Create the Supabase Storage bucket for signed LOIs (public). Idempotent.'

    def handle(self, *args, **opts):
        base = os.getenv('SUPABASE_URL', '').rstrip('/')
        key = os.getenv('SUPABASE_SERVICE_KEY', '')
        bucket = os.getenv('SUPABASE_BUCKET', 'loi')
        if not base or not key:
            self.stderr.write(self.style.ERROR('Set SUPABASE_URL and SUPABASE_SERVICE_KEY in the backend env first.'))
            return
        r = requests.post(
            f'{base}/storage/v1/bucket',
            json={'id': bucket, 'name': bucket, 'public': True},
            headers={'Authorization': f'Bearer {key}', 'apikey': key, 'Content-Type': 'application/json'},
            timeout=20,
        )
        if r.status_code in (200, 201):
            self.stdout.write(self.style.SUCCESS(f'Bucket "{bucket}" created (public).'))
        elif r.status_code == 409 or 'already exists' in r.text.lower() or 'duplicate' in r.text.lower():
            self.stdout.write(f'Bucket "{bucket}" already exists — nothing to do.')
        else:
            self.stderr.write(self.style.ERROR(f'Create failed ({r.status_code}): {r.text[:300]}'))
