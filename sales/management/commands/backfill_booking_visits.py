"""Give every already-approved booking the lead and site visit it should have had.

New approvals do this themselves (see _ensure_lead_and_site_visit_for_booking).
This walks the bookings approved before that existed, so the historical funnel
matches the current one instead of showing closures that came from nowhere.

Reuses the same helper as the live path, so there is exactly one definition of
what "the lead and visit behind a booking" means — a backfill that drifted from
the runtime rule would be worse than no backfill.

    manage.py backfill_booking_visits --dry-run          # report, change nothing
    manage.py backfill_booking_visits                    # apply
    manage.py backfill_booking_visits --company 1
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from sales.models import Booking, Lead, SiteVisit


class Command(BaseCommand):
    help = 'Backfill the lead + completed site visit behind already-approved bookings.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change and roll back.')
        parser.add_argument('--company', type=int, default=None,
                            help='Limit to one company id.')

    def handle(self, *args, **opts):
        from sales.views import _ensure_lead_and_site_visit_for_booking, _drop_superseded_revisions

        qs = Booking.objects.filter(status='sold')
        if opts['company']:
            qs = qs.filter(company_id=opts['company'])
        # Superseded revisions share their parent's lead and unit; backfilling them
        # too would ask for a second visit for one sale.
        qs = _drop_superseded_revisions(qs).order_by('id')

        leads_before, visits_before = Lead.objects.count(), SiteVisit.objects.count()
        made_lead = made_visit = had_visit = skipped = failed = 0

        try:
            with transaction.atomic():
                for b in qs.select_related('stm', 'plot'):
                    before_lead = b.lead_id
                    try:
                        # Its own savepoint: on Postgres a failed statement poisons the
                        # whole transaction, so without this one bad row would take the
                        # rest of the run down with it.
                        with transaction.atomic():
                            lead_id, sv_id = _ensure_lead_and_site_visit_for_booking(b)
                    except Exception as e:                      # noqa: BLE001
                        failed += 1
                        self.stderr.write('  booking %s failed: %s: %s'
                                          % (b.pk, type(e).__name__, e))
                        continue
                    if lead_id is None:
                        skipped += 1
                        continue
                    if not before_lead:
                        made_lead += 1
                    if sv_id:
                        made_visit += 1
                    else:
                        had_visit += 1

                self.stdout.write('bookings examined        : %d' % qs.count())
                self.stdout.write('  leads attached/created : %d' % made_lead)
                self.stdout.write('  site visits created    : %d' % made_visit)
                self.stdout.write('  already had a visit    : %d' % had_visit)
                self.stdout.write('  skipped (no date/name) : %d' % skipped)
                self.stdout.write('  failed                 : %d' % failed)
                self.stdout.write('leads   %d -> %d' % (leads_before, Lead.objects.count()))
                self.stdout.write('visits  %d -> %d' % (visits_before, SiteVisit.objects.count()))

                if opts['dry_run']:
                    raise _Rollback()
        except _Rollback:
            self.stdout.write(self.style.WARNING('DRY RUN — everything above was rolled back.'))
            return
        self.stdout.write(self.style.SUCCESS('Backfill applied.'))


class _Rollback(Exception):
    """Used to unwind the transaction on a dry run."""
