"""Audit the one rule that matters on the unit map: a unit a live booking holds is
never free, and no unit carries two live sales.

Every write path is guarded and the map reconciles itself on read, but a guard is
only as good as the code that calls it. This asks the database directly, so it stays
true regardless of which path wrote last — run it after a deploy, on a schedule, or
whenever a figure looks wrong.

    python manage.py check_unit_integrity            # report only, exits 1 on drift
    python manage.py check_unit_integrity --fix      # and correct it
"""
from django.core.management.base import BaseCommand

from sales.models import Booking, Plot
from sales.views import _drop_superseded_revisions


class Command(BaseCommand):
    help = 'Report (and optionally fix) units whose status disagrees with their bookings.'

    def add_arguments(self, parser):
        parser.add_argument('--fix', action='store_true',
                            help='Correct what it finds instead of only reporting.')
        parser.add_argument('--company', help='Limit to one company code.')

    def handle(self, *args, **o):
        bookings = Booking.objects.all()
        plots = Plot.objects.select_related('project')
        if o.get('company'):
            bookings = bookings.filter(company__code=o['company'])
            plots = plots.filter(project__company__code=o['company'])

        # Which units a live booking holds, and whether that booking is a completed
        # sale or one still waiting on an approver.
        claim = {}
        for b in bookings.filter(status__in=('pending', 'sold')).only(
                'id', 'plot_id', 'plot_ids', 'status', 'client_name'):
            held = set(b.plot_ids or [])
            if b.plot_id:
                held.add(b.plot_id)
            for pid in held:
                if b.status == 'sold' or pid not in claim:
                    claim[pid] = b

        # 1. a unit somebody bought, sitting on the map as free
        loose = list(plots.filter(id__in=claim.keys(), status='available'))
        # 2. a unit carrying two live sales at once
        live = _drop_superseded_revisions(bookings).filter(status='sold')
        # A resale legitimately leaves the earlier sale on the unit — it happened, and
        # it stays on file. The booking that replaced it says so, so the one it points
        # at is not a second live sale.
        resold = set(bookings.filter(is_resale=True, resale_of__isnull=False)
                     .values_list('resale_of_id', flat=True))
        per_unit = {}
        for b in live.only('id', 'plot_id', 'client_name'):
            if b.plot_id and b.id not in resold:
                per_unit.setdefault(b.plot_id, []).append(b)
        doubled = {pid: bs for pid, bs in per_unit.items() if len(bs) > 1}

        for p in loose:
            b = claim[p.id]
            self.stdout.write(self.style.WARNING(
                f'  LOOSE   {p.project.name}/{p.number} reads available, held by '
                f'#{b.id} {b.client_name} ({b.status})'))
        for pid, bs in doubled.items():
            p = Plot.objects.filter(id=pid).select_related('project').first()
            self.stdout.write(self.style.ERROR(
                f'  DOUBLED {p.project.name}/{p.number} has {len(bs)} live sales: '
                + ', '.join(f'#{b.id} {b.client_name}' for b in bs)))

        if o['fix'] and loose:
            for p in loose:
                b = claim[p.id]
                Plot.objects.filter(id=p.id).update(
                    status='sold' if b.status == 'sold' else 'hold',
                    held_by=None, held_at=None, pre_hold_status='')
            self.stdout.write(self.style.SUCCESS(f'Corrected {len(loose)} loose unit(s).'))

        # A double sale is never corrected automatically: only a person knows which of
        # the two buyers is the real one, and guessing would cancel somebody's deal.
        if doubled:
            self.stdout.write(self.style.ERROR(
                'Double sales are NOT auto-corrected — cancel the wrong booking from '
                'Closures, since only you know which buyer is the real one.'))

        if not loose and not doubled:
            self.stdout.write(self.style.SUCCESS('Unit map is consistent with its bookings.'))
            return
        raise SystemExit(1)
