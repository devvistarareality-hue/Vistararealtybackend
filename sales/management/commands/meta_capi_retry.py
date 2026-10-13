"""Re-send lead stages that Meta did not accept (network trouble, Meta down).

    python manage.py meta_capi_retry

Events older than 7 days are given up on (Meta rejects them), and each one is tried
at most MAX_ATTEMPTS times.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from sales import meta_capi
from sales.models import MetaCapiEvent


class Command(BaseCommand):
    help = 'Re-send failed Meta Conversions API lead events.'

    def handle(self, *args, **opts):
        week = timezone.now() - timedelta(days=7)
        qs = MetaCapiEvent.objects.filter(status__in=('failed', 'pending'), attempts__lt=meta_capi.MAX_ATTEMPTS,
                                          event_time__gte=week)
        n = 0
        for ev_id in qs.values_list('id', flat=True)[:500]:
            meta_capi.send(ev_id)
            n += 1
        self.stdout.write(f'Retried {n} event(s).')
