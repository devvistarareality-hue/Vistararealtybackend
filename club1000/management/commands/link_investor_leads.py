"""Give every approved Club 1000 investment its own lead (one-time backfill).

Investors approved before leads were made on approval have none, and two
investments could share one lead because matching was by mobile number alone.
This walks the approved investors oldest first and, for each one, keeps its lead
if no earlier investment already holds it; otherwise finds or makes one with the
same rule approval now uses (club1000.views._lead_for_investor: same number, same
scheme, not held by another investment). Every linked lead is marked converted,
with a history row.

    python manage.py link_investor_leads --dry-run   # report only, writes nothing
    python manage.py link_investor_leads
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from club1000.models import Investor, Lead, LeadStatusHistory
from club1000.views import _lead_for_investor


class _DryRun(Exception):
    pass


class Command(BaseCommand):
    help = 'Give every approved Club 1000 investment its own lead.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report what would change; write nothing.')

    def handle(self, *args, dry_run=False, **opts):
        counts = {'kept': 0, 'linked_existing': 0, 'created': 0, 'unshared': 0, 'converted': 0, 'no_phone': 0}
        try:
            with transaction.atomic():
                self._run(counts)
                if dry_run:
                    raise _DryRun()
        except _DryRun:
            pass
        tag = '[dry-run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f"{tag}kept {counts['kept']} · linked to an existing lead {counts['linked_existing']} · "
            f"new leads {counts['created']} · split off a shared lead {counts['unshared']} · "
            f"marked converted {counts['converted']} · no mobile {counts['no_phone']}"))

    def _run(self, counts):
        held = set()
        investors = (Investor.objects.filter(approval_status='approved')
                     .select_related('company', 'scheme', 'added_by').order_by('investment_date', 'id'))
        for inv in investors:
            if inv.lead_id and inv.lead_id not in held:
                counts['kept'] += 1
            else:
                if inv.lead_id:          # an earlier investment already holds this lead
                    Investor.objects.filter(pk=inv.pk).update(lead=None)
                    inv.lead_id, inv.lead = None, None
                    counts['unshared'] += 1
                before = set(Lead.objects.filter(company=inv.company).values_list('id', flat=True))
                lead = _lead_for_investor(inv, None)
                if lead is None:
                    counts['no_phone'] += 1
                    continue
                counts['created' if lead.id not in before else 'linked_existing'] += 1
            held.add(inv.lead_id)
            lead = Lead.objects.get(pk=inv.lead_id)
            if lead.status != 'converted':
                old = lead.status
                Lead.objects.filter(pk=lead.pk).update(status='converted', next_follow_up_date=None)
                LeadStatusHistory.objects.create(lead_id=lead.pk, changed_by=None, field_changed='status',
                                                 old_value=old, new_value='converted',
                                                 remarks='Converted — linked to its approved investment')
                counts['converted'] += 1
