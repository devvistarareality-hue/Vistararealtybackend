"""Investor portfolios — one person, everything they hold with the club.

A portfolio is every investment under one mobile number, whatever names they were
entered under (the names are listed with it). It gives
how many times they have invested (each investment plus its renewals), the schemes
it is spread over, what is active now and ever invested, what has been paid out and
is still due, and the people they brought into the club: investors whose reference
is their number, and the referral rewards earned for them.

Built in Python: names, phones and amounts are encrypted, so none of this can be
grouped or summed in SQL.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Payout, ReferralReward
from .permissions import has_club1000_access
from .services import money_in, normalize_phone, past_terms

COUNTED = ('approved', 'pending')      # drafts and rejections are not holdings


def _people(investors):
    """[[investor, …], …] — one list per mobile number, oldest investment first."""
    by_phone = defaultdict(list)
    for inv in investors:
        by_phone[normalize_phone(inv.phone) or f'#{inv.pk}'].append(inv)
    return [sorted(rows, key=lambda i: (_began(i), i.pk)) for rows in by_phone.values()]


def _began(inv):
    """When this money first came in — before any renewal restarted the term."""
    return inv.first_invested_on or inv.investment_date or inv.created_at.date()


def _names(person):
    """The distinct names a number was entered under, most-invested first."""
    from .views import _same_person
    names = []
    for inv in sorted(person, key=lambda i: -_money(i.amount_invested)):
        if not any(_same_person(n, inv.name) for n in names):
            names.append(inv.name)
    return names


def _money(v):
    return v if isinstance(v, Decimal) else Decimal(str(v or 0))


def _split(inv, pays):
    """One investment's money in principal and interest, the ledger's way: interest is
    everything scheduled beyond the principal; a payout of the 'interest' type is all
    interest, and a maturity / redemption row repays principal first, the rest interest."""
    principal = money_in(inv)
    total = sum((_money(p.amount_due) for p in pays), Decimal('0'))
    amt = lambda p: _money(p.paid_amount if p.paid_amount is not None else p.amount_due)
    paid = sum((amt(p) for p in pays if p.status == 'paid'), Decimal('0'))
    paid_back = sum((amt(p) for p in pays if p.status == 'paid' and p.payout_type != 'interest'), Decimal('0'))
    principal_paid = min(paid_back, principal)
    pending = sum((_money(p.amount_due) for p in pays if p.status != 'paid'), Decimal('0'))
    pending_back = sum((_money(p.amount_due) for p in pays if p.status != 'paid' and p.payout_type != 'interest'), Decimal('0'))
    principal_pending = min(pending_back, max(principal - principal_paid, Decimal('0')))
    return {
        'principal': principal, 'interest': max(total - principal, Decimal('0')) if pays else Decimal('0'),
        'principal_paid': principal_paid, 'interest_paid': paid - principal_paid,
        'principal_pending': principal_pending, 'interest_pending': pending - principal_pending,
    }


def _summary(person, payouts_by_inv, referred_by_phone, rewards_by_phone):
    approved = [i for i in person if i.approval_status == 'approved']
    first = person[0]
    names = _names(person)
    phone = normalize_phone(first.phone)
    schemes = defaultdict(lambda: {'count': 0, 'active': Decimal('0'), 'total': Decimal('0')})
    for i in approved:
        sc = schemes[i.scheme.name if i.scheme_id else '—']
        sc['count'] += 1
        sc['total'] += money_in(i)
        if i.status == 'active':
            sc['active'] += _money(i.amount_invested)
    paid = pending = overdue = Decimal('0')
    money = defaultdict(lambda: Decimal('0'))
    today = date.today()
    for i in approved:
        pays = payouts_by_inv.get(i.pk, [])
        for k, v in _split(i, pays).items():
            money[k] += v
        for p in pays:
            if p.status == 'paid':
                paid += _money(p.paid_amount if p.paid_amount is not None else p.amount_due)
            else:
                pending += _money(p.amount_due)
                if p.due_date and p.due_date <= today:
                    overdue += _money(p.amount_due)
    referred = referred_by_phone.get(phone, []) if phone else []
    rewards = rewards_by_phone.get(phone, []) if phone else []
    renewals = sum((i.renewal_count or 0) for i in approved)
    return {
        'id': first.pk,
        'name': names[0] if names else first.name, 'names': names,
        'phone': first.phone, 'email': next((i.email for i in person if i.email), ''),
        'since': min(_began(i) for i in person),
        'investments': len(approved),
        'awaiting_approval': sum(1 for i in person if i.approval_status == 'pending'),
        'renewals': renewals,
        'times_invested': len(approved) + renewals,
        'schemes': [{'scheme': k, 'count': v['count'], 'active': str(v['active']), 'total': str(v['total'])}
                    for k, v in sorted(schemes.items(), key=lambda kv: -kv[1]['active'])],
        'active_amount': str(sum((_money(i.amount_invested) for i in approved if i.status == 'active'), Decimal('0'))),
        'total_invested': str(sum((money_in(i) for i in approved), Decimal('0'))),
        'paid_out': str(paid),
        'principal_total': str(money['principal']), 'interest_total': str(money['interest']),
        'principal_paid': str(money['principal_paid']), 'interest_paid': str(money['interest_paid']),
        'principal_pending': str(money['principal_pending']), 'interest_pending': str(money['interest_pending']),
        'pending_payouts': str(pending),
        'due_now': str(overdue),
        'referrals': len({r.pk for r in referred}),
        'referred_amount': str(sum((_money(r.amount_invested) for r in referred), Decimal('0'))),
        'rewards_earned': str(sum((_money(w.amount) for w in rewards), Decimal('0'))),
        'rewards_paid': str(sum((_money(w.paid_amount if w.paid_amount is not None else w.amount) for w in rewards if w.status == 'paid'), Decimal('0'))),
    }


def _load(request):
    """Everything a portfolio needs, scoped like the Investors list."""
    from .views import _investor_visibility_qs
    investors = list(_investor_visibility_qs(request).filter(approval_status__in=COUNTED))
    ids = [i.pk for i in investors]
    payouts_by_inv = defaultdict(list)
    for p in Payout.objects.filter(investor_id__in=ids):
        payouts_by_inv[p.investor_id].append(p)
    referred_by_phone = defaultdict(list)
    for i in investors:
        ref = normalize_phone(i.reference_phone)
        if ref and i.approval_status == 'approved' and ref != normalize_phone(i.phone):   # not themselves
            referred_by_phone[ref].append(i)
    rewards_by_phone = defaultdict(list)
    for w in ReferralReward.objects.filter(investor_id__in=ids).select_related('investor', 'investor__scheme'):
        ref = normalize_phone(w.reference_phone)
        if ref and ref != normalize_phone(w.investor.phone):
            rewards_by_phone[ref].append(w)
    return investors, payouts_by_inv, referred_by_phone, rewards_by_phone


class PortfolioListView(APIView):
    """GET every investor's portfolio summary, biggest active holding first."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return Response({'detail': 'No Club 1000 access.'}, status=status.HTTP_403_FORBIDDEN)
        investors, payouts_by_inv, referred_by_phone, rewards_by_phone = _load(request)
        rows = [_summary(p, payouts_by_inv, referred_by_phone, rewards_by_phone) for p in _people(investors)]
        from sales.ai_capture import ai_capture
        if ai_capture(request, rows=[{**r, 'names': ', '.join(r['names']),
                                      'schemes': ', '.join(x['scheme'] for x in r['schemes'])} for r in rows]):
            return Response({})
        q = (request.query_params.get('search') or '').strip().lower()
        if q:
            digits = ''.join(c for c in q if c.isdigit())
            rows = [r for r in rows if any(q in (n or '').lower() for n in r['names']) or (digits and digits in ''.join(c for c in (r['phone'] or '') if c.isdigit()))]
        rows.sort(key=lambda r: (-Decimal(r['active_amount']), -r['times_invested'], (r['name'] or '').lower()))
        return Response(rows)


class PortfolioDetailView(APIView):
    """GET one person's portfolio — any of their investments' ids finds them."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return Response({'detail': 'No Club 1000 access.'}, status=status.HTTP_403_FORBIDDEN)
        investors, payouts_by_inv, referred_by_phone, rewards_by_phone = _load(request)
        person = next((p for p in _people(investors) if any(i.pk == pk for i in p)), None)
        if not person:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        out = _summary(person, payouts_by_inv, referred_by_phone, rewards_by_phone)
        # What 'Add more' carries into a new investment for this person — from their latest one.
        latest = max(person, key=lambda i: (i.investment_date or date.min, i.pk))
        out['prefill'] = {'name': latest.name, 'phone': latest.phone, 'email': latest.email or '', 'pan': latest.pan or '',
                          'source': latest.source or 'referral', 'reference_name': latest.reference_name or '',
                          'reference_phone': latest.reference_phone or ''}
        out['history'] = []
        for i in sorted(person, key=lambda i: (i.investment_date, i.pk), reverse=True):
            pays = payouts_by_inv.get(i.pk, [])
            sp = _split(i, pays)
            out['history'].append({
                **{k: str(v) for k, v in sp.items()},
                'id': i.pk, 'name': i.name, 'scheme': i.scheme.name if i.scheme_id else '—', 'loi_no': i.loi_no or '',
                'first_invested_on': _began(i),
                'past_terms': [{'start': t['start'], 'maturity': t['maturity'], 'principal': str(t['principal']),
                                'renewed_with': str(t['next']), 'taken_out': str(t['returned'] + t['interest'] - t['rolled'])}
                               for t in past_terms(i)],
                'amount': str(i.amount_invested), 'investment_date': i.investment_date, 'maturity_date': i.maturity_date,
                'interest_payout': i.interest_payout, 'return_pct': str(i.total_return_pct or ''),
                'status': i.status, 'approval_status': i.approval_status, 'renewal_count': i.renewal_count or 0,
                'paid_out': str(sum((_money(p.paid_amount if p.paid_amount is not None else p.amount_due) for p in pays if p.status == 'paid'), Decimal('0'))),
                'pending': str(sum((_money(p.amount_due) for p in pays if p.status != 'paid'), Decimal('0'))),
            })
        phone = normalize_phone(person[0].phone)
        referred = referred_by_phone.get(phone, []) if phone else []
        rewards = rewards_by_phone.get(phone, []) if phone else []
        reward_of = defaultdict(list)
        for w in rewards:
            reward_of[w.investor_id].append(w)
        out['referred'] = [{
            'id': r.pk, 'name': r.name, 'phone': r.phone, 'scheme': r.scheme.name if r.scheme_id else '—',
            'amount': str(r.amount_invested), 'investment_date': r.investment_date, 'status': r.status,
            'reward': str(sum((_money(w.amount) for w in reward_of.get(r.pk, [])), Decimal('0'))),
            'reward_status': ('paid' if reward_of.get(r.pk) and all(w.status == 'paid' for w in reward_of[r.pk])
                              else 'pending' if reward_of.get(r.pk) else ''),
        } for r in sorted(referred, key=lambda r: (r.investment_date or person[0].investment_date), reverse=True)]
        return Response(out)
