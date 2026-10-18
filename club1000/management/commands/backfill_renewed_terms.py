"""Rebuild the finished terms of investments renewed before terms were kept.

A renewal used to overwrite the term in place: its dates were lost from the record
and, for an at-maturity scheme, its maturity payout (and with it the interest earned)
was deleted. The activity log still has each renewal approval's before → after, so
each finished term is rebuilt from it and its payouts are put back as due.

    python manage.py backfill_renewed_terms            # show what would change
    python manage.py backfill_renewed_terms --apply    # save it
"""
import json
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from activity.models import ActivityLog
from club1000.models import Investor, Payout
from club1000.services import close_term


def _details(log):
    d = log.details or {}
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = {}
    return d if isinstance(d, dict) else {}


def _from(changes, field):
    for c in changes or []:
        if c.get('field') == field:
            return c.get('from')
    return None


class Command(BaseCommand):
    help = 'Rebuild finished terms (and their payouts) of investments renewed before terms were kept.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        todo = [i for i in Investor.objects.filter(renewal_count__gt=0).select_related('scheme')
                if len(i.past_terms or []) < (i.renewal_count or 0)]
        if not todo:
            self.stdout.write('Nothing to rebuild.')
            return
        for inv in todo:
            logs = ActivityLog.objects.filter(target_type='investor', target_id=str(inv.pk), action='approve').order_by('created_at')
            renewals = [l for l in logs if _from(_details(l).get('changes'), 'Investment date')]
            terms = []
            for l in renewals:
                ch = _details(l).get('changes')
                start, maturity = _from(ch, 'Investment date'), _from(ch, 'Maturity date')
                amount = _from(ch, 'Amount invested')
                pct = _from(ch, 'Total return pct')
                payout = _from(ch, 'Interest payout')
                terms.append({'start': start, 'maturity': maturity,
                              'principal': str(Decimal(str(amount).replace(',', ''))) if amount not in (None, '—') else None,
                              'return_pct': pct if pct not in (None, '—') else None,
                              'interest_payout': (payout or '').lower() if payout not in (None, '—') else None})
            # Fill what a log did not change from the term after it (or today's).
            nxt = {'principal': str(inv.amount_invested), 'return_pct': str(inv.total_return_pct or 0),
                   'interest_payout': inv.interest_payout}
            for t in reversed(terms):
                for k in ('principal', 'return_pct', 'interest_payout'):
                    t[k] = t[k] or nxt[k]
                nxt = t
            self.stdout.write(f'{inv.pk} {inv.name} · {inv.scheme.name} · LOI {inv.loi_no} — renewed {inv.renewal_count}×, '
                              f'{len(terms)} finished term(s) found in the log:')
            for n, t in enumerate(terms):
                after = terms[n + 1]['principal'] if n + 1 < len(terms) else str(inv.amount_invested)
                self.stdout.write(f'   term {n}: {t["start"]} → {t["maturity"]} · ₹{Decimal(t["principal"]):,.0f} · '
                                  f'{t["return_pct"]}% {t["interest_payout"]} · renewed with ₹{Decimal(after):,.0f}')
            if not opts['apply'] or not terms:
                continue
            with transaction.atomic():
                inv.past_terms = (inv.past_terms or []) + terms
                if not inv.first_invested_on:
                    inv.first_invested_on = date.fromisoformat(terms[0]['start'])
                inv.save(update_fields=['past_terms', 'first_invested_on'])
                for n, t in enumerate(terms):
                    after = Decimal(terms[n + 1]['principal'] if n + 1 < len(terms) else str(inv.amount_invested))
                    paid = Payout.objects.filter(investor=inv, payout_type='maturity', status='paid',
                                                 due_date=date.fromisoformat(t['maturity'])).exists()
                    close_term(inv, t, after, maturity_paid=paid)
                    for p in Payout.objects.filter(investor=inv, due_date=date.fromisoformat(t['maturity']), status='pending'):
                        self.stdout.write(f'      + {p.payout_type} ₹{p.amount_due:,.0f} due {p.due_date} ({p.notes})')
        if not opts['apply']:
            self.stdout.write('\nNothing saved — run with --apply to save.')
