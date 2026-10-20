"""The investor statement a client can be given — the Ledger, term by term, as a
print-ready HTML page in the same layout as AR's Statement of Account. The web
prints it to PDF in the browser; the app turns it into a PDF with expo-print."""
import re
from datetime import date

from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from .models import Investor
from .permissions import has_club1000_access, is_club1000_manager

SETTLED = re.compile(r'^(?:Settled to|Settled at|Added to) renewal R(\d+)', re.I)
PAYOUT_NAME = {'monthly': 'Monthly', 'quarterly': 'Quarterly', 'maturity': 'At maturity'}


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _fmt(s):
    d = _d(s)
    return d.strftime('%d/%m/%Y') if d else '—'


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _inr(n):
    from .views import _inr as f
    return f(n)


class InvestorStatementView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from .views import InvestorLedgerView, _company_filtered, _no_access
        if not has_club1000_access(request.user):
            return _no_access()
        resp = InvestorLedgerView().get(request, pk)      # same figures as the Ledger, same access
        if resp.status_code != 200:
            return resp
        qs = _company_filtered(Investor.objects.select_related('scheme', 'company'), request)
        if not is_club1000_manager(request.user):
            qs = qs.filter(added_by=request.user)
        investor = qs.get(pk=pk)
        data = resp.data
        today = date.today()
        terms = data['terms'] or [{'no': 0, 'label': 'Original', 'start': str(investor.investment_date),
                                   'maturity': str(investor.maturity_date), 'principal': str(investor.amount_invested),
                                   'current': True, 'summary': data['summary']}]
        sections = []
        overdue = 0.0
        red_no = 0
        for t in terms:
            rows = []
            for e in data['entries']:
                if e['type'] == 'investment' or (e.get('term') or 0) != t['no']:
                    continue
                due = _d(e.get('due_date') or e['date'])
                m = SETTLED.match(str(e.get('notes') or '')) if e['status'] == 'paid' else None
                if m:
                    status, tone = f'Settled to Renewal {m.group(1)}', 'info'
                elif e['status'] == 'paid':
                    early = e.get('paid_date') and _d(e['paid_date']) < due
                    status, tone = ('Paid early' if early else 'Paid'), 'ok'
                elif due and due <= today:
                    status, tone = 'Due', 'bad'
                    overdue += _num(e['amount'])
                else:
                    status, tone = 'Upcoming', 'off'
                period = e.get('period') or {}
                rd = e.get('redemption') or {}
                name = t['label'].split(' · ')[0]
                if e['type'] == 'premature_redemption':
                    # A redemption is its own step inside the term — headed as such.
                    red_no += 1
                    rows.append({'head': f"Redemption {red_no} · {_fmt(rd.get('redemption_date') or due)} — "
                                         f"{_inr(_num(rd.get('redeem_amount') or e['amount']))} taken out of {name}"
                                         + (f" · {_inr(_num(rd.get('remaining_principal')))} stays invested"
                                            if rd.get('remaining_principal') not in (None, '') else '')})
                # Handed to him at the renewal (not carried into it): say so.
                at_renewal = bool(m) and not e.get('carried') and 'added to the principal' not in str(e.get('notes') or '').lower()
                if at_renewal:
                    status, tone = f'Paid at Renewal {m.group(1)}', 'ok'
                rows.append({
                    'label': ('Principal paid back' if at_renewal and e['type'] == 'maturity' else e['label']), 'due': _fmt(due), 'amount': _num(e['amount']),
                    'paid': _num(e['paid_amount']) if e.get('paid_amount') is not None else None,
                    'paid_on': _fmt(e.get('paid_date')) if e['status'] == 'paid' else '—',
                    'status': status, 'tone': tone,
                    'added': 'added to the principal' in str(e.get('notes') or '').lower(),
                    'carried': bool(e.get('carried')),
                    'working': (('Not a payment — ' + ('added into' if e['type'] == 'interest' else 'renewed into') + ' the next term')
                                if e.get('carried') else
                                'Paid to him at the renewal' if at_renewal else
                                (lambda ms: f"{_inr(_num(ms['principal']))} principal + {_inr(_num(ms['interest']))} interest")(e['maturity_split'])
                                if e.get('maturity_split') else
                                f"Redeemed on {_fmt(rd.get('redemption_date') or due)}"
                                + (f" · held {rd.get('days_held')} days" if rd.get('days_held') is not None else '')
                                if e['type'] == 'premature_redemption' else
                                f"{period.get('label')} · {period.get('days')} days × ₹{_num(period.get('per_day')):,.2f} a day"
                                if period.get('days') else ''),
                })
            s = t.get('summary') or {}
            mo = t.get('made_of') or {}
            carried = sum((r['amount'] for r in rows if r.get('carried')), 0.0)
            # money rows only — the redemption headings carry no amount
            money_rows = [r for r in rows if not r.get('head')]
            for n, r in enumerate(money_rows, start=1):
                r['no'] = n
            sections.append({
                # this renewal's money, as it was made up at the renewal
                'made_of': {k: _num(v) for k, v in mo.items()} if mo else None,
                # rows total includes what was carried into the next term (not paid out)
                'rows_total': sum((r['amount'] for r in money_rows), 0.0), 'carried': carried,
                'next_label': (terms[terms.index(t) + 1]['label'].split(' · ')[0] if terms.index(t) + 1 < len(terms) else ''),
                'label': t['label'], 'current': t.get('current'), 'start': _fmt(t['start']), 'maturity': _fmt(t['maturity']),
                'principal': _num(t['principal']), 'rows': rows,
                'total': _num(s.get('total')), 'paid_out': _num(s.get('paid_out')), 'pending': _num(s.get('pending')),
                'interest': _num(s.get('interest')),
                # how a finished term ended
                'renewed_with': _num(t.get('renewed_with')) if t.get('renewed_with') else None,
                'interest_added': _num(t.get('interest_added')), 'fresh_money': _num(t.get('fresh_money')),
                'taken_back': _num(t.get('principal_returned')) if not t.get('current') else 0,
            })
        sm = data['summary']
        cur = next((t for t in terms if t.get('current')), terms[-1])
        state = ('Due for renewal' if investor.is_matured() else
                 {'active': 'Active', 'redeemed': 'Redeemed', 'premature_redeemed': 'Redeemed early'}.get(
                     investor.status, investor.get_status_display()))
        html = render_to_string('club1000/statement.html', {
            'company': investor.company, 'inv': investor, 'scheme': investor.scheme.name if investor.scheme_id else '—',
            'payout': PAYOUT_NAME.get(investor.interest_payout, investor.interest_payout),
            'pct': f'{_num(investor.total_return_pct):g}', 'state': state, 'state_tone': 'ok' if state == 'Active' else 'warn',
            'first_on': _fmt(terms[0]['start']), 'term_now': f"{_fmt(cur['start'])} → {_fmt(cur['maturity'])}",
            'renewals': len(terms) - 1, 'sections': sections, 'multi': len(terms) > 1,
            'principal': _num(sm.get('principal')), 'interest': _num(sm.get('interest')), 'total': _num(sm.get('total')),
            'paid_out': _num(sm.get('paid_out')), 'pending': _num(sm.get('pending')), 'overdue': overdue,
            'as_of': today.strftime('%d/%m/%Y'), 'generated': timezone.localtime().strftime('%d/%m/%Y %I:%M %p'),
        })
        try:
            from activity.recorder import note
            note(request, f'Statement downloaded — {investor.name}{f" · LOI {investor.loi_no}" if investor.loi_no else ""}',
                 action='statement', target_type='investor', target_id=str(investor.pk), module='Club 1000')
        except Exception:
            pass
        return HttpResponse(html, content_type='text/html; charset=utf-8')
