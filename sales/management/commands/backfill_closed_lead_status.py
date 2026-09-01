"""Sync Lead.status to 'closed' wherever stm_status is already 'closed'.

New approvals do this themselves now (see BookingActionView.post, which used
to update only stm_status, leaving a lead auto-created at booking submission
stranded on status='new' forever). This walks the leads closed before that
fix existed, so All Leads and the dashboard's closed-count tile (both read
off `status`) agree with what the STM portal already shows.

    manage.py backfill_closed_lead_status --dry-run          # report, change nothing
    manage.py backfill_closed_lead_status                    # apply
    manage.py backfill_closed_lead_status --company 1
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from sales.models import Lead


class Command(BaseCommand):
    help = "Backfill Lead.status='closed' wherever stm_status is already 'closed'."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change and roll back.')
        parser.add_argument('--company', type=int, default=None,
                            help='Limit to one company id.')

    def handle(self, *args, **opts):
        qs = Lead.objects.filter(stm_status='closed').exclude(status='closed')
        if opts['company']:
            qs = qs.filter(company_id=opts['company'])

        count = qs.count()
        try:
            with transaction.atomic():
                updated = qs.update(status='closed')
                self.stdout.write('leads matched (stm_status=closed, status!=closed): %d' % count)
                self.stdout.write('leads updated to status=closed                  : %d' % updated)
                if opts['dry_run']:
                    raise _Rollback()
        except _Rollback:
            self.stdout.write(self.style.WARNING('DRY RUN — everything above was rolled back.'))
            return
        self.stdout.write(self.style.SUCCESS('Backfill applied.'))


class _Rollback(Exception):
    """Used to unwind the transaction on a dry run."""
