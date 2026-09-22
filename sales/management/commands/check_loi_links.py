"""Open every stored LOI through a fresh signed link and report any that fail.

Read-only: signs short-lived links and requests each PDF; nothing is changed.

Usage:  python manage.py check_loi_links [--since-days N]
"""
from datetime import timedelta

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from sales.models import Booking
from sales.supabase_storage import create_signed_url


class Command(BaseCommand):
    help = 'Verify every stored LOI opens through its signed link.'

    def add_arguments(self, parser):
        parser.add_argument('--since-days', type=int, default=0,
                            help='Only bookings created in the last N days (0 = all).')

    def handle(self, *args, **opts):
        qs = Booking.objects.exclude(loi_document='').exclude(loi_document__isnull=True).only('id', 'loi_document')
        if opts['since_days']:
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=opts['since_days']))
        ok, bad = 0, []
        for b in qs.iterator():
            url = create_signed_url(b.loi_document.name, 60)
            try:
                code = requests.get(url, stream=True, timeout=20).status_code if url else 'no link'
            except requests.RequestException as e:
                code = type(e).__name__
            if code == 200:
                ok += 1
            else:
                bad.append((b.id, code, b.loi_document.name))
        for bid, code, name in bad:
            self.stdout.write(self.style.ERROR(f'booking {bid}: {code}  {name}'))
        self.stdout.write(self.style.SUCCESS(f'{ok} LOIs open') + (self.style.ERROR(f', {len(bad)} failing') if bad else ''))
