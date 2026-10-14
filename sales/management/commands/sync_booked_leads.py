"""Safety net: set to Closed every lead whose current booking is approved by Sales and
Accounts but whose lead status says otherwise. Normally nothing to do (sales/lead_sync.py
handles it on save) — run it after an import or to check.

    python manage.py sync_booked_leads [--dry-run]
"""
from django.core.management.base import BaseCommand

from sales.lead_sync import _approved, close_lead_for
from sales.models import Booking, Lead


class Command(BaseCommand):
    help = 'Close leads whose booking is fully approved.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, dry_run=False, **opts):
        from sales.views import _drop_superseded_revisions
        current = _drop_superseded_revisions(Booking.objects.exclude(lead__isnull=True).exclude(status='draft'))
        fixed = 0
        for b in current.only('id', 'lead_id', 'status', 'accounts_status', 'approval_status', 'cancelled_at'):
            if not _approved(b):
                continue
            if Lead.objects.filter(pk=b.lead_id).exclude(status='closed', stm_status='closed').exists():
                fixed += 1
                if not dry_run:
                    close_lead_for(b.id)
        self.stdout.write(f"{'Would close' if dry_run else 'Closed'} {fixed} lead(s).")
