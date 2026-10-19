import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.views import APIView
from sales.ai_capture import ai_capture
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from accounts.models import User
from accounts.permissions import is_platform_admin, scope_to_company
from sales.fields import phone_blind_index
from sales.views import MANAGER_ROLES
from .permissions import is_club1000_manager, has_club1000_access, scope_leads_to_role, _scheme_approver_ids, can_approve_investor, can_configure_scheme_approvers, approval_gate, is_level2_approver, can_delete_investors
from .models import (
    Scheme, Investor, Payout, ReferralReward, Lead, FollowUp, LeadStatusHistory,
    REFERRAL_REWARD_PCT, REINVESTMENT_REFERRAL_REWARD_PCT, PAYOUT_TYPE_CHOICES,
)
from .serializers import (
    SchemeSerializer, InvestorListSerializer,
    InvestorCreateSerializer, PayoutSerializer, ReferralRewardSerializer,
    LeadListSerializer, LeadCreateSerializer, FollowUpSerializer, LeadStatusHistorySerializer,
)
from .services import (past_terms, maturity_value, generate_payout_schedule, generate_past_term_payouts, money_in, normalize_phone, interest_periods,
                       close_term, term_list, term_of)


def _company_filtered(qs, request, field='company'):
    """scope_to_company + honour ?company_id for platform admins (existing sales convention)."""
    qs = scope_to_company(qs, request.user, field)
    cid = request.query_params.get('company_id')
    if cid and is_platform_admin(request.user):
        qs = qs.filter(**{f'{field}_id': cid})
    return qs


def _no_access():
    return Response({'detail': 'You do not have access to Club 1000.'}, status=status.HTTP_403_FORBIDDEN)


def scope_investors(qs, user, prefix=''):
    """Whose investors this person sees, honouring the designation's record scope.

    Club 1000 has always split on manager-or-not: a manager sees the desk, an
    executive sees what they added. A company that sets the scope on the
    designation gets that instead — own, their team's, or the whole company —
    and leaving it unset keeps the old split exactly.
    """
    from accounts.capabilities import (SCOPE_COMPANY, SCOPE_OWN, SCOPE_TEAM, data_scope)
    # A platform admin sees the whole desk whatever their designation says. They
    # are the account owner, and a designation set to "own records only" would
    # otherwise hide every other employee's work from the person answerable for
    # all of it.
    if is_platform_admin(user):
        return qs
    field = f'{prefix}added_by'
    scope = data_scope(user)
    if scope == SCOPE_COMPANY:
        return qs
    if scope == SCOPE_OWN:
        return qs.filter(**{field: user})
    if scope == SCOPE_TEAM:
        from sales.views import _visible_user_ids
        return qs.filter(**{f'{field}__in': _visible_user_ids(user)})
    return None            # nothing configured: the caller keeps its old rule


def club_can(user, key):
    """Inside Club 1000, what this person may do — their company's designation
    settings decide (accounts/capabilities.py). The manager gate still applies:
    capabilities refine it, they don't replace it."""
    from accounts.capabilities import user_can
    return is_club1000_manager(user) and user_can(user, key)


def _no_permission():
    return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)


class StatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()

        # Only an approved investor is an investor. Until someone signs off, the
        # money is not committed — a pending one may be rejected and a draft was
        # never even submitted, yet both used to land in total_invested, the
        # investor count, the per-scheme split and the top-investors list. A
        # rejected one counted too, which meant a refused deal permanently
        # overstated the book. pending_approval_count below is deliberately
        # separate: counting what is waiting is its whole job.
        investors = _company_filtered(
            Investor.objects.select_related('scheme'), request).filter(approval_status='approved')
        manager = is_club1000_manager(request.user)
        _scoped = scope_investors(investors, request.user)
        if _scoped is not None:
            investors = _scoped
        elif not manager:
            investors = investors.filter(added_by=request.user)

        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        # Scheme filter (?scheme=1,2): every figure on the dashboard — the book, the
        # portfolio snapshot, leads, follow-ups, approvals — for just those schemes.
        scheme_ids = [int(x) for x in str(request.query_params.get('scheme') or '').split(',') if x.strip().isdigit()]
        if scheme_ids:
            investors = investors.filter(scheme_id__in=scheme_ids)
        if date_from:
            investors = investors.filter(investment_date__gte=date_from)
        if date_to:
            investors = investors.filter(investment_date__lte=date_to)

        total_invested = Decimal('0')
        by_scheme = {}
        for inv in investors:
            total_invested += inv.amount_invested or Decimal('0')
            key = inv.scheme.name
            entry = by_scheme.setdefault(key, {'scheme': key, 'investors': 0, 'amount': Decimal('0')})
            entry['investors'] += 1
            entry['amount'] += inv.amount_invested or Decimal('0')

        payouts = Payout.objects.filter(investor__in=investors)
        pending = payouts.filter(status='pending')
        paid = payouts.filter(status='paid')
        pending_amount = sum((p.amount_due or Decimal('0') for p in pending), Decimal('0'))
        paid_amount = sum((p.amount_due or Decimal('0') for p in paid), Decimal('0'))

        leads = scope_leads_to_role(_company_filtered(Lead.objects.all(), request), request.user)
        if scheme_ids:
            leads = leads.filter(scheme_interest_id__in=scheme_ids)
        if date_from:
            leads = leads.filter(lead_date__gte=date_from)
        if date_to:
            leads = leads.filter(lead_date__lte=date_to)
        leads_count = leads.count()
        converted_count = leads.filter(status='converted').count()

        # Portfolio-state figures below are deliberately NOT scoped to the
        # date filter above — "how many investors need a renew/payout
        # decision right now" or "what's coming due" is a live snapshot, not
        # a fact about investments made in a chosen date range. Same
        # ownership scoping as `investors`, just without the date clause.
        portfolio = _company_filtered(
            Investor.objects.select_related('scheme'), request).filter(approval_status='approved')
        if not manager:
            portfolio = portfolio.filter(added_by=request.user)
        if scheme_ids:
            portfolio = portfolio.filter(scheme_id__in=scheme_ids)

        # is_matured() is computed on the fly (nothing ever flips status to
        # 'matured' — see the model) — an investor sitting active past its
        # maturity date genuinely needs a Renew/Payout decision, which is a
        # more useful bucket than the raw, effectively-unused 'matured' choice.
        status_breakdown = {'active': 0, 'due_for_renewal': 0, 'redeemed': 0, 'premature_redeemed': 0}
        for inv in portfolio:
            if inv.status == 'active' and inv.is_matured():
                status_breakdown['due_for_renewal'] += 1
            else:
                status_breakdown[inv.status] = status_breakdown.get(inv.status, 0) + 1

        today = date.today()
        soon = today + timedelta(days=30)
        upcoming = portfolio.filter(status='active', maturity_date__gte=today, maturity_date__lte=soon)
        upcoming_amount = sum((i.amount_invested or Decimal('0') for i in upcoming), Decimal('0'))

        followups = _company_filtered(FollowUp.objects.all(), request, field='lead__company').filter(status='pending')
        if not manager:
            followups = followups.filter(assigned_to=request.user)
        if scheme_ids:
            followups = followups.filter(lead__scheme_interest_id__in=scheme_ids)
        now = timezone.now()
        followups_overdue = followups.filter(scheduled_at__lt=now).count()
        followups_today = followups.filter(scheduled_at__date=today).count()

        referrals = ReferralReward.objects.filter(investor__in=portfolio)
        referral_pending = referrals.filter(status='pending')
        referral_paid_amount = sum(((r.paid_amount if r.paid_amount is not None else r.amount) or Decimal('0') for r in referrals.filter(status='paid')), Decimal('0'))
        referral_pending_amount = sum((r.amount or Decimal('0') for r in referral_pending), Decimal('0'))

        data = {
            'total_invested': total_invested,
            'investor_count': investors.count(),
            'pending_payout_count': pending.count(),
            'pending_payout_amount': pending_amount,
            'paid_payout_count': paid.count(),
            'paid_payout_amount': paid_amount,
            'by_scheme': list(by_scheme.values()),
            'leads_count': leads_count,
            'converted_count': converted_count,
            'investor_status_breakdown': status_breakdown,
            'upcoming_maturities_count': upcoming.count(),
            'upcoming_maturities_amount': upcoming_amount,
            'followups_due_today': followups_today,
            'followups_overdue': followups_overdue,
            'referral_pending_count': referral_pending.count(),
            'referral_pending_amount': referral_pending_amount,
            'referral_paid_amount': referral_paid_amount,
            # Live snapshot, like the other portfolio figures above — what is
            # coming is not a fact about investments made in a chosen range, so
            # it reads `portfolio` rather than the date-filtered `investors`.
            'payout_forecast': _payout_forecast(portfolio, today),
        }
        if manager:
            data['active_scheme_count'] = _company_filtered(Scheme.objects.filter(is_active=True), request).count()
            pending_q = _investor_visibility_qs(request).filter(Q(approval_status='pending') | Q(pending_action__isnull=False))
            if scheme_ids:
                pending_q = pending_q.filter(scheme_id__in=scheme_ids)
            data['pending_approval_count'] = pending_q.count()
            # Sorted here, not by the database: amount_invested is encrypted, so an
            # ORDER BY sorted the ciphertext and the "top" five came out in any order.
            top = sorted(portfolio, key=lambda i: i.amount_invested or Decimal('0'), reverse=True)[:5]
            data['top_investors'] = [
                {'name': i.name, 'amount': i.amount_invested, 'scheme': i.scheme.name, 'status': i.status}
                for i in top
            ]
        return Response(data)


class SchemeListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        schemes = _company_filtered(Scheme.objects.filter(is_active=True), request).order_by('tenure_months')
        return Response(SchemeSerializer(schemes, many=True).data)

    def post(self, request):
        if not is_club1000_manager(request.user):
            return _no_permission()
        ser = SchemeSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        scheme = ser.save(company=request.user.company, created_by=request.user)
        return Response(SchemeSerializer(scheme).data, status=status.HTTP_201_CREATED)


class SchemeDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        return _company_filtered(Scheme.objects.all(), request).filter(pk=pk).first()

    def patch(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        # investor_approvers specifically needs the narrower config-admin check
        # (Director/real admin) — everything else on a scheme stays editable by
        # any Club 1000 manager, same as before.
        if 'investor_approvers' in request.data and not can_configure_scheme_approvers(request.user):
            return _no_permission()
        scheme = self._get(request, pk)
        if not scheme:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = SchemeSerializer(scheme, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        scheme = self._get(request, pk)
        if not scheme:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if scheme.investors.exists():
            scheme.is_active = False
            scheme.save(update_fields=['is_active'])
            return Response({'detail': 'Scheme has investors — disabled instead of deleted.'})
        scheme.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SchemeToggleApproverView(APIView):
    """Add/remove one manager from a scheme's investor_approvers, atomically.

    The obvious client-side approach — read investor_approvers, compute the
    new array, PATCH the whole thing — races the moment two toggles for the
    SAME scheme are in flight together (e.g. checking two managers a click
    apart in the UI): each request computed `next` from whatever it read
    before either had saved, so whichever response's write lands LAST at the
    DB wins, regardless of which was sent last, silently dropping the other
    click's change with no error on either side. select_for_update() forces
    concurrent toggles for the same scheme to serialize at the DB row lock,
    each one reading the true post-previous-toggle state, so nothing is lost
    no matter how the requests interleave over the network."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not can_configure_scheme_approvers(request.user):
            return _no_permission()
        manager_id = request.data.get('manager_id')
        if not manager_id:
            return Response({'detail': 'manager_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            scheme = _company_filtered(Scheme.objects.select_for_update(), request).filter(pk=pk).first()
            if not scheme:
                return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
            field = 'investor_approvers_l2' if str(request.data.get('level')) == '2' else 'investor_approvers'
            current = getattr(scheme, field) or []
            manager_id = int(manager_id)
            setattr(scheme, field, [m for m in current if m != manager_id] if manager_id in current else [*current, manager_id])
            scheme.save(update_fields=[field])
        return Response(SchemeSerializer(scheme).data)


class ReferenceSuggestionsView(APIView):
    """Distinct reference name/number pairs already used at this company, one
    per phone number (earliest spelling wins) — powers the Add Investor
    autocomplete so a name isn't re-typed with different casing each time."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(Investor.objects.exclude(reference_phone=''), request).order_by('created_at')
        seen = {}
        for name, phone in qs.values_list('reference_name', 'reference_phone'):
            key = normalize_phone(phone)
            if key and key not in seen:
                seen[key] = {'reference_name': name, 'reference_phone': phone}
        return Response(list(seen.values()))


class Club1000UsersView(APIView):
    """Users eligible for Club 1000 Lead assignment — anyone with Club 1000
    access at all (manager or plain module access), scoped via
    has_club1000_access. Mirrors sales.TelecallerListView's role for this
    module, but Club 1000 has no telecaller/STM/CP split — it's just "has
    access or not" — so no designation-based sub-filtering is needed."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        # has_club1000_access -> is_platform_admin reads user.company.code, so without
        # the join this walked back to the database once per user in the list.
        users = (User.objects.filter(company=request.user.company, is_active=True)
                 .select_related('company').order_by('name'))
        # ?approvers=1 — the approver pickers: Club 1000 managers and every Director (a
        # Director can be appointed an approver in any module — sales.views.is_director).
        if request.query_params.get('approvers') == '1':
            from sales.views import is_director
            return Response([
                {'id': u.id, 'name': u.name, 'user_code': u.user_code, 'role': u.role, 'designation': u.designation,
                 'club_manager': is_club1000_manager(u), 'director': is_director(u),
                 'has_access': has_club1000_access(u)}
                for u in users if is_club1000_manager(u) or is_director(u)
            ])
        data = [
            {'id': u.id, 'name': u.name, 'user_code': u.user_code, 'role': u.role, 'designation': u.designation,
             # can act as an approver (approving needs manager-level Club 1000 access)
             'club_manager': is_club1000_manager(u)}
            for u in users if has_club1000_access(u)
        ]
        return Response(data)


def _name_words(name):
    return [w for w in re.split(r'[^a-z0-9]+', (name or '').lower()) if w]


def _same_person(lead_name, investor_name):
    """Do a lead's name and an investor's name belong to one person? Every word of
    the shorter name must be in the longer one, so "Ketan Shah" matches "KETAN SHAH -
    REF JIGNESH SHAH - FORTUNE 55" but not "Tejas Mogra" on the same shared number."""
    a, b = _name_words(lead_name), _name_words(investor_name)
    if not a or not b:
        return False
    short, long_ = (a, set(b)) if len(a) <= len(b) else (b, set(a))
    return all(w in long_ for w in short)


def _ensure_converted_lead(investor, actor):
    """On any approval: the investment's lead — found by number + name, or made
    from the investor's details — marked converted, with a history row."""
    lead = _lead_for_investor(investor, actor)
    if lead is None:
        return None
    if lead.status != 'converted':
        old = lead.status
        Lead.objects.filter(pk=lead.pk).update(status='converted', next_follow_up_date=None)
        LeadStatusHistory.objects.create(
            lead_id=lead.pk, changed_by=actor, field_changed='status', old_value=old,
            new_value='converted', remarks='Converted via Investor approval')
    return lead


def _lead_for_investor(investor, actor):
    """The Club 1000 lead behind an approved investor, found or made — one lead per
    investment.

    An investor added straight from the Add Investor form has no lead, so the
    funnel showed none of them. Every investment came from someone enquiring,
    whether or not anybody typed a lead first.

    One person can invest many times, in the same scheme or another, under one
    mobile number — and one number is sometimes shared by several clients (23
    investments on one live number). So a phone match alone is not a person: an
    investment takes a lead with the same number, the same name AND the same scheme (or an enquiry
    that named no scheme) that no other investment already holds, preferring one
    dated on the investment date; failing that a lead is made for it. Two investments therefore never share one lead, and
    a second investment never borrows the first one's history.

    Matched through phone_key, the blind index — `phone` is encrypted and cannot be
    looked up directly — within the company. Returns the lead, or None if there is
    nothing to go on.
    """
    if investor.lead_id:
        return investor.lead
    key = phone_blind_index(investor.phone)
    if not key:
        return None
    taken = Investor.objects.exclude(pk=investor.pk).exclude(lead__isnull=True).values('lead_id')
    free = Lead.objects.filter(company=investor.company, phone_key=key).exclude(pk__in=taken).order_by('created_at')
    # Same number AND same person: names are encrypted, so compared here in Python.
    free = [l for l in free if _same_person(l.name, investor.name)]
    same_scheme = [l for l in free if l.scheme_interest_id == investor.scheme_id]
    # An enquiry that named no scheme can be this investment too; one that named
    # another scheme belongs to that one.
    lead = (next((l for l in same_scheme if l.lead_date == investor.investment_date), None)
            or next(iter(same_scheme), None)
            or next((l for l in free if l.scheme_interest_id is None), None))
    if lead is None:
        lead = Lead.objects.create(
            company=investor.company,
            name=investor.name,
            phone=investor.phone,
            email=investor.email or '',
            source=investor.source or 'other',
            reference_name=investor.reference_name or '',
            reference_phone=investor.reference_phone or '',
            lead_date=investor.investment_date,
            scheme_interest=investor.scheme,
            amount_interested=investor.amount_invested,
            total_return_pct=investor.total_return_pct,
            assigned_to=investor.added_by,
            created_by=actor,
            status='new',          # flipped to converted by the caller, with history
            remarks='Created from a direct investor entry.',
        )
    # Tie the two together so a later approval or revision finds it again.
    Investor.objects.filter(pk=investor.pk).update(lead=lead)
    investor.lead = lead
    return lead


def _distinct(qs, field):
    """Every value `field` takes in `qs`, as strings, blanks left out — what a
    filter picker may offer (?facets=1), so it never lists a choice that
    returns nothing."""
    return sorted({str(v) for v in qs.order_by().values_list(field, flat=True).distinct() if v not in (None, '')})


def _payout_forecast(portfolio, as_of, months=3):
    """Pending payouts grouped into the current month, the next two, then
    everything later, then anything undated — the same shape AR's "Falling due"
    uses (receivables/engine.py::compute), so the two dashboards read alike.

    The months are built from `as_of` rather than stored anywhere, so the
    columns move with the calendar: in November the first column is Nov.

    Only what is NOT yet due, matching AR: a payout whose date has passed is
    overdue, which is a different question from what is coming. Undated
    payouts cannot happen today (due_date is non-null), so that column is
    appended only if one ever appears rather than sitting there empty.
    """
    pending = Payout.objects.filter(investor__in=portfolio, status='pending')

    labels, y, m = [], as_of.year, as_of.month
    for _ in range(months):
        labels.append(date(y, m, 1))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    names = [d.strftime('%b-%y') for d in labels]
    last_month_start = labels[-1]

    # amount_due is encrypted, so this cannot be a SQL GROUP BY — the rows are
    # summed in Python, the same reason _sum_money exists on the Sales side.
    buckets = {n: Decimal('0') for n in names}
    after = Decimal('0')
    undated = Decimal('0')
    for due, amount in pending.values_list('due_date', 'amount_due'):
        amount = amount or Decimal('0')
        if due is None:
            undated += amount
        elif due <= as_of:
            continue                      # overdue, not "falling due"
        elif date(due.year, due.month, 1) > last_month_start:
            after += amount
        else:
            buckets[due.strftime('%b-%y')] += amount

    out = [{'label': n, 'amount': buckets[n]} for n in names]
    out.append({'label': f'After {names[-1]}', 'amount': after})
    if undated:
        out.append({'label': 'No date', 'amount': undated})
    return out


def _digits(value):
    """Just the digits of a phone number, so +91 98765 43210 and 9876543210
    compare equal. Used where the column is encrypted and has no blind index to
    match on."""
    return ''.join(c for c in str(value or '') if c.isdigit())


def _parse_date(value):
    """A date from the client, or None when it is missing or unparseable."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _draft_decimal(value, fallback):
    """A number a half-filled form may simply not have yet."""
    if value in (None, ''):
        return fallback or 0
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return fallback or 0


class InvestorListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(Investor.objects.select_related('scheme', 'added_by'), request)
        scoped = scope_investors(qs, request.user)
        if scoped is not None:
            qs = scoped
        elif not is_club1000_manager(request.user):
            approver_scheme_ids = _scheme_approver_ids(request.user, request.user.company)
            if approver_scheme_ids:
                qs = qs.filter(Q(added_by=request.user) | Q(scheme_id__in=approver_scheme_ids))
            else:
                qs = qs.filter(added_by=request.user)
        # A draft is unfinished work, not a record: only its author sees one,
        # whatever scheme-approver or manager rights the viewer holds. The
        # exception is a platform admin, who can see any employee's data in this
        # module — they own the account and answer for what is in it.
        if not is_platform_admin(request.user):
            qs = qs.exclude(Q(approval_status='draft') & ~Q(added_by=request.user))
        if ai_capture(request, qs=qs.exclude(approval_status__in=('rejected', 'draft'))):
            return Response({})
        if request.query_params.get('facets') == '1':
            # The pool the page lists from, before its own filters.
            pool = qs
            approval = request.query_params.get('approval_status')
            if approval and approval != 'all':
                pool = pool.filter(approval_status=approval)
            elif not approval:
                pool = pool.exclude(approval_status__in=('rejected', 'draft'))
            added = [{'id': str(u['added_by_id']), 'name': u['added_by__name'] or '—'}
                     for u in pool.exclude(added_by__isnull=True)
                     .order_by().values('added_by_id', 'added_by__name').distinct()]
            return Response({
                'scheme_ids': _distinct(pool, 'scheme_id'),
                'statuses': _distinct(pool, 'status'),
                'payouts': _distinct(pool, 'interest_payout'),
                'added_by': sorted(added, key=lambda a: a['name']),
            })
        if request.query_params.get('scheme_id'):
            qs = qs.filter(scheme_id=request.query_params['scheme_id'])
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        # The two dashboard tiles that count something no stored status holds.
        # 'Due for renewal' is active-and-past-maturity (Investor.is_matured, which
        # is computed because nothing ever flips status to 'matured'), and
        # 'Maturing in 30 days' is a window on the date. Both are the same
        # conditions the dashboard counts with — kept together so a tile and the
        # list it opens cannot disagree about what it was counting.
        # The dashboard filters its figures by investment date and by who added
        # them (its Mine / Employee / Manager toggle). The list could do neither,
        # so a tile's number had no list that matched it. Same fields, same
        # meaning — investment_date, as the dashboard uses.
        d_from = _parse_date(request.query_params.get('date_from'))
        d_to = _parse_date(request.query_params.get('date_to'))
        if d_from:
            qs = qs.filter(investment_date__gte=d_from)
        if d_to:
            qs = qs.filter(investment_date__lte=d_to)
        if request.query_params.get('added_by'):
            qs = qs.filter(added_by_id=request.query_params['added_by'])
        if request.query_params.get('interest_payout'):
            qs = qs.filter(interest_payout=request.query_params['interest_payout'])
        bucket = request.query_params.get('bucket')
        if bucket == 'due_for_renewal':
            qs = qs.filter(status='active', maturity_date__isnull=False,
                           maturity_date__lte=date.today())
        elif bucket == 'maturing_soon':
            qs = qs.filter(status='active', maturity_date__gte=date.today(),
                           maturity_date__lte=date.today() + timedelta(days=30))
        approval_status = request.query_params.get('approval_status')
        if approval_status == 'all':
            pass  # Approvals page's "All" tab — no filter, includes rejected.
        elif approval_status == 'rejected':
            # Rejected investments, and approved ones whose revision or renewal was
            # turned down (they stay on their old terms; the rejection is shown).
            qs = qs.filter(Q(approval_status='rejected') | Q(approval_status='approved', rejections__isnull=False))
        elif approval_status == 'pending':
            # Waiting on an approver: new / revised investments, and requested deletes / undos.
            qs = qs.filter(Q(approval_status='pending') | Q(pending_action__isnull=False))
        elif approval_status:
            qs = qs.filter(approval_status=approval_status)
        else:
            # Default view excludes rejected investors — they only show up under
            # an explicit ?approval_status=rejected (or =all) filter. Drafts are
            # left out for the same reason and one more: nothing has been agreed
            # yet, so counting one as an investor would overstate the book.
            qs = qs.exclude(approval_status__in=('rejected', 'draft'))
        search = request.query_params.get('search', '').strip()
        if search:
            # name/phone/email are encrypted, so SQL can't match them. A full
            # ten-digit number uses the blind index; anything else is matched in
            # Python over the already-scoped rows and fed back as an id filter.
            digits = ''.join(c for c in search if c.isdigit())
            if len(digits) >= 10 and not any(c.isalpha() for c in search):
                qs = qs.filter(phone_key=phone_blind_index(digits))
            else:
                needle = search.lower()
                hits = [pk for pk, nm, ph, em in qs.values_list('id', 'name', 'phone', 'email')
                        if needle in (nm or '').lower() or needle in (ph or '').lower()
                        or needle in (em or '').lower()]
                qs = qs.filter(Q(id__in=hits) | Q(loi_no__icontains=search))
        return Response(InvestorListSerializer(qs.order_by('-created_at'), many=True).data)

    def post(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        ser = InvestorCreateSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        scheme = ser.validated_data['scheme']
        if scheme.company_id and scheme.company_id != request.user.company_id and not is_platform_admin(request.user):
            return Response({'detail': 'Invalid scheme for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        lead = ser.validated_data.get('lead')
        if lead and lead.company_id != request.user.company_id and not is_platform_admin(request.user):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        investment_date = ser.validated_data.get('investment_date') or date.today()
        maturity_date = investment_date + relativedelta(months=scheme.tenure_months)
        # Interest payout & return % are prefilled from the scheme but editable
        # per-investor — only fall back to the scheme's default if the caller
        # didn't send an explicit value.
        interest_payout = ser.validated_data.get('interest_payout') or (
            scheme.interest_payout_options[0] if scheme.interest_payout_options else 'maturity'
        )
        total_return_pct = ser.validated_data.get('total_return_pct')
        if total_return_pct is None:
            # payout_rates holds text ("15.00") — make it a number before it is used.
            total_return_pct = Decimal(str(scheme.rate_for(interest_payout) or 0))

        # Canonicalize the reference by phone number — free-typed names drift
        # ("chinmay" vs "Chinmay"), but a phone number is stable. If this phone
        # was already used, reuse the earliest name/phone on file for it instead
        # of creating a fresh spelling variant.
        reference_name = ser.validated_data.get('reference_name', '')
        reference_phone = ser.validated_data.get('reference_phone', '')
        normalized = normalize_phone(reference_phone)
        if normalized:
            prior = _company_filtered(Investor.objects.exclude(reference_phone=''), request).order_by('created_at')
            for name, phone in prior.values_list('reference_name', 'reference_phone'):
                if normalize_phone(phone) == normalized:
                    reference_name, reference_phone = name, phone
                    break

        # Submitting a saved draft promotes that same row instead of creating a
        # second one — otherwise the draft would be left behind as an orphan, and
        # the same client would appear twice.
        draft = None
        if request.data.get('draft_id'):
            owned = Q(added_by=request.user)
            if is_platform_admin(request.user):
                owned = Q()
            draft = (_company_filtered(Investor.objects.all(), request)
                     .filter(Q(pk=request.data['draft_id'], approval_status='draft') & owned).first())
            if draft:
                ser.instance = draft

        # Custom payout-schedule edits (if any) are held until approval, not
        # generated now — generate_payout_schedule runs in InvestorActionView.approve.
        payout_schedule = request.data.get('payout_schedule')
        investor = ser.save(
            company=request.user.company,
            added_by=request.user,
            investment_date=investment_date,
            maturity_date=maturity_date,
            interest_payout=interest_payout,
            total_return_pct=total_return_pct,
            reference_name=reference_name,
            reference_phone=reference_phone,
            approval_status='pending',
            pending_payout_schedule=payout_schedule if isinstance(payout_schedule, list) else None,
        )
        # NOTE: the source Lead is NOT marked converted, no Payout schedule is
        # generated, and no ReferralReward is created here — all three are
        # deferred to approval (InvestorActionView.approve), mirroring Sales'
        # Closure-only-on-approval precedent. A rejected investor leaves no trace.

        # Scan document, sent as base64 {name, type, data} — same convention as
        # sales' signed-LOI upload (BookingListCreateView).
        doc = request.data.get('document_file')
        if isinstance(doc, dict) and doc.get('data'):
            import base64
            from django.core.files.base import ContentFile
            ext = (doc.get('name') or '').rsplit('.', 1)[-1][:10] or 'bin'
            doc_path = f'investor_{investor.id}_doc.{ext}'
            try:
                investor.document.save(doc_path, ContentFile(base64.b64decode(doc['data'])), save=True)
            except Exception:
                # Don't silently orphan the document: log and relink the deterministic path.
                import logging
                logging.getLogger(__name__).exception('Investor document save failed for %s', investor.id)
                try:
                    investor.document.name = f'club1000/{doc_path}'
                    investor.save(update_fields=['document'])
                except Exception:
                    logging.getLogger(__name__).exception('Investor document relink failed for %s', investor.id)
        # Signed LOI, sent as base64 {name, type, data} — same convention as
        # sales' signed-LOI upload in BookingListCreateView.post; supersedes the
        # separate InvestorUploadLoiView round-trip as the primary path.
        loi = request.data.get('loi_file')
        if isinstance(loi, dict) and loi.get('data'):
            import base64
            from django.core.files.base import ContentFile
            if not investor.loi_no:
                investor.loi_no = _next_investor_loi_no(investor.company, investor.scheme)
                investor.save(update_fields=['loi_no'])
            try:
                investor.loi_document.save(_investor_loi_path(investor), ContentFile(base64.b64decode(loi['data'])), save=True)
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Investor LOI save failed for %s', investor.id)
        _notify_investor_approvers(request.user.company, investor, request.user)
        return Response(InvestorListSerializer(investor).data, status=status.HTTP_201_CREATED)


class InvestorDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        qs = _company_filtered(Investor.objects.select_related('scheme', 'added_by'), request)
        if not is_club1000_manager(request.user):
            qs = qs.filter(added_by=request.user)
        return qs.filter(pk=pk).first()

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = self._get(request, pk)
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(InvestorListSerializer(investor).data)

    def patch(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = self._get(request, pk)
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = InvestorListSerializer(investor, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        ser.save()
        return Response(ser.data)

    def delete(self, request, pk):
        """Delete an investment — managers and above only. See _delete_investor."""
        if not has_club1000_access(request.user):
            return _no_access()
        investor = self._get(request, pk)
        # Anyone may delete their own draft (unsent work); everything else is managers only.
        own_draft = bool(investor and investor.approval_status == 'draft' and investor.added_by_id == request.user.id)
        if not can_delete_investors(request.user) and not own_draft:
            return Response({'detail': 'Only a Manager (or above) can delete an investor.'}, status=status.HTTP_403_FORBIDDEN)
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        include_paid = str(request.query_params.get('include_paid') or request.data.get('include_paid') or '').lower() in ('1', 'true', 'yes')
        n_pay, amt_pay, n_rew, amt_rew = _paid_on(investor)
        if (n_pay or n_rew) and not include_paid:
            # The screen asks "delete these payments too?" and sends include_paid.
            bits = [f'{n_pay} payout{"s" if n_pay != 1 else ""} (₹{amt_pay:,.0f})' if n_pay else '',
                    f'{n_rew} referral reward{"s" if n_rew != 1 else ""} (₹{amt_rew:,.0f})' if n_rew else '']
            return Response({'detail': f'{" and ".join(b for b in bits if b)} for this investor already paid.', 'code': 'has_paid',
                             'paid_payouts': n_pay, 'paid_amount': str(amt_pay),
                             'paid_rewards': n_rew, 'paid_rewards_amount': str(amt_rew)}, status=status.HTTP_409_CONFLICT)
        _delete_investor(request, investor, include_paid=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _paid_on(investor):
    """What has already been paid out on an investment: (payouts, ₹, rewards, ₹)."""
    amt = lambda o: o.paid_amount if o.paid_amount is not None else (getattr(o, 'amount_due', None) or getattr(o, 'amount', None) or Decimal('0'))
    pays = [p for p in investor.payouts.all() if p.status == 'paid']
    rews = [r for r in investor.referral_rewards.all() if r.status == 'paid']
    return (len(pays), sum((amt(p) for p in pays), Decimal('0')), len(rews), sum((amt(r) for r in rews), Decimal('0')))


def _request_action(request, investor, kind, reason, include_paid=False):
    """File a delete / undo-renewal for the scheme's approvers instead of doing it."""
    from activity.recorder import note
    investor.pending_action = {'type': kind, 'by': request.user.id, 'by_name': request.user.name or '',
                               'at': timezone.now().isoformat(), 'reason': reason, 'include_paid': bool(include_paid)}
    investor.save(update_fields=['pending_action'])
    what = 'Delete' if kind == 'delete' else 'Undo renewal'
    note(request, f'{what} requested — {investor.name}{f" · LOI {investor.loi_no}" if investor.loi_no else ""}: {reason}',
         action='delete-request' if kind == 'delete' else 'undo-renewal-request', target_type='investor',
         target_id=str(investor.pk), module='Club 1000')
    try:
        from notifications import notify, reporting_chain
        ids = (investor.scheme.investor_approvers if investor.scheme_id else None) or []
        to = list(User.objects.filter(id__in=ids, company=investor.company, is_active=True)) if ids else []
        if not to:
            to = list(User.objects.filter(company=investor.company, is_active=True).filter(Q(role='Admin') | Q(is_staff=True)))
        for u in {u.id: u for u in to}.values():
            notify(u, 'investor_approval', f'{what} needs approval',
                   f'{investor.name} · {investor.scheme.name if investor.scheme_id else ""} — by {request.user.name}: {reason}',
                   {'investor_id': investor.id})
    except Exception:
        pass


def _delete_investor(request, investor, include_paid=False):
    """Delete one investment; returns (why not, what was paid) or (None, what was paid)
    once it is gone. Its schedule, rewards and stored documents go with it, and its lead
    stops reading Converted — it reads Investor Deleted.

    Money already paid out (payouts or referral rewards marked paid) is a book of
    record, so the delete stops and says so unless `include_paid` — the person has
    been shown what will go and confirmed it; then those payments go too and the log
    keeps what they were."""
    n_pay, amt_pay, n_rew, amt_rew = paid = _paid_on(investor)
    if (n_pay or n_rew) and not include_paid:
        bits = []
        if n_pay:
            bits.append(f'{n_pay} payout{"s" if n_pay != 1 else ""} (₹{amt_pay:,.0f})')
        if n_rew:
            bits.append(f'{n_rew} referral reward{"s" if n_rew != 1 else ""} (₹{amt_rew:,.0f})')
        why = f'{" and ".join(bits)} for this investor {"are" if n_pay + n_rew > 1 else "is"} already paid.'
        return why, paid
    from activity.recorder import note
    pk = investor.pk
    label = f'{investor.name}{f" · LOI {investor.loi_no}" if investor.loi_no else ""} · {investor.scheme.name} · ₹{investor.amount_invested:,.0f}'
    files = [f for f in (investor.loi_document, investor.pending_loi_document, investor.document) if f]
    lead = investor.lead
    with transaction.atomic():
        investor.delete()
        if lead and lead.status == 'converted' and not lead.investor_conversions.exists():
            Lead.objects.filter(pk=lead.pk).update(status='investor_deleted')
            LeadStatusHistory.objects.create(lead=lead, changed_by=request.user, field_changed='status',
                                             old_value='converted', new_value='investor_deleted',
                                             remarks=f'Investor deleted — {label}')
    for f in files:
        try:
            f.storage.delete(f.name)
        except Exception:
            pass
    gone = []
    if n_pay:
        gone.append(f'{n_pay} paid payout{"s" if n_pay != 1 else ""} (₹{amt_pay:,.0f})')
    if n_rew:
        gone.append(f'{n_rew} paid referral reward{"s" if n_rew != 1 else ""} (₹{amt_rew:,.0f})')
    note(request, f'Deleted {"draft " if investor.approval_status == "draft" else ""}investor — {label}' + (f' · with {" and ".join(gone)} deleted too' if gone else ''),
         action='deleted', target_type='investor', target_id=str(pk), module='Club 1000')
    return None, paid


class InvestorBulkDeleteView(APIView):
    """POST {ids: [...]} — delete several investments at once, managers only. Each
    one follows the single-delete rule; those with money already paid are left
    alone and listed with the reason."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        manager = can_delete_investors(request.user)
        ids = request.data.get('ids') or []
        try:
            ids = sorted({int(i) for i in ids})
        except (TypeError, ValueError):
            return Response({'detail': 'ids must be a list of investor ids.'}, status=status.HTTP_400_BAD_REQUEST)
        if not ids:
            return Response({'detail': 'Pick at least one investor.'}, status=status.HTTP_400_BAD_REQUEST)
        include_paid = str(request.data.get('include_paid') or '').lower() in ('1', 'true', 'yes')
        qs = _company_filtered(Investor.objects.select_related('scheme', 'lead'), request).filter(pk__in=ids)
        if not manager:
            # Anyone may delete their own drafts (unsent work); everything else is managers only.
            qs = qs.filter(approval_status='draft', added_by=request.user)
            if not qs.exists():
                return Response({'detail': 'Only a Manager (or above) can delete investors.'}, status=status.HTTP_403_FORBIDDEN)
        found = {i.pk: i for i in qs}
        deleted, refused = [], []
        for pk in ids:
            inv = found.get(pk)
            if not inv:
                refused.append({'id': pk, 'name': '', 'reason': 'Not found.'})
                continue
            name = inv.name
            n_pay, amt_pay, n_rew, amt_rew = _paid_on(inv)
            if (n_pay or n_rew) and not include_paid:
                refused.append({'id': pk, 'name': name, 'reason': 'Payments already made.', 'paid_payouts': n_pay,
                                'paid_amount': str(amt_pay), 'paid_rewards': n_rew, 'paid_rewards_amount': str(amt_rew)})
            else:
                was_draft = inv.approval_status == 'draft'
                _delete_investor(request, inv, include_paid=True)
                deleted.append({'id': pk, 'name': name, 'draft': was_draft})
        # One entry for the whole action — every investor deleted, and every one refused
        # with why (each delete above replaced the previous note, so this is the record).
        from activity.recorder import note
        parts = []
        if deleted:
            # Say what was deleted: drafts (unsent work) or investments.
            n_draft = sum(1 for d in deleted if d.get('draft'))
            n = len(deleted)
            what = (f'draft investor{"s" if n != 1 else ""}' if n_draft == n
                    else f'investor{"s" if n != 1 else ""}' + (f' ({n_draft} of them drafts)' if n_draft else ''))
            parts.append(f'Deleted {n} {what} — ' + ', '.join(d['name'] for d in deleted))
        if refused:
            parts.append(f'kept {len(refused)} (payments already made — not confirmed) — ' + ', '.join(r['name'] or f'#{r["id"]}' for r in refused))
        if parts:
            note(request, ' · '.join(parts), action='deleted' if deleted else 'delete-refused', target_type='investor',
                 module='Club 1000', details={'deleted': [d['id'] for d in deleted], 'refused': [r['id'] for r in refused]})
        return Response({'deleted': [d['id'] for d in deleted], 'refused': refused})


class InvestorLedgerView(APIView):
    """Full money-movement history for one investor, in one chronological
    timeline: the investment itself plus every scheduled payout (interest,
    maturity, premature redemption) — paid or still pending. Everything the
    client has put in and everything owed/paid back to them."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(Investor.objects.select_related('scheme', 'added_by'), request)
        if not is_club1000_manager(request.user):
            qs = qs.filter(added_by=request.user)
        investor = qs.filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        payout_labels = dict(PAYOUT_TYPE_CHOICES)
        # Each term (the original investment and every renewal) is its own ledger:
        # its entries carry `term`, and `terms` gives each one's own figures.
        terms = term_list(investor)
        entries = [{
            'type': 'investment',
            'payout_id': None,
            'date': str(t['start']),
            'label': (f'Investment — {investor.scheme.name}' if t['no'] == 0 else f'Renewal {t["no"]} — {investor.scheme.name}'),
            'amount': str(t['principal']),
            'paid_amount': None,
            'status': 'completed',
            'paid_date': str(t['start']),
            'notes': '',
            'term': t['no'],
        } for t in terms] or [{
            'type': 'investment', 'payout_id': None, 'date': str(investor.investment_date),
            'label': f'Investment — {investor.scheme.name}', 'amount': str(investor.amount_invested),
            'paid_amount': None, 'status': 'completed', 'paid_date': str(investor.investment_date), 'notes': '', 'term': 0,
        }]
        total_due = Decimal('0')
        total_paid = Decimal('0')
        all_payouts = list(investor.payouts.all())
        periods = interest_periods(investor, all_payouts)
        for p in all_payouts:
            actual_paid = p.paid_amount if p.paid_amount is not None else p.amount_due
            entries.append({
                'type': p.payout_type,
                'payout_id': p.id,
                'date': str(p.due_date),
                'label': payout_labels.get(p.payout_type, p.payout_type) + (' — balance' if p.balance_of_id else ''),
                'amount': str(p.amount_due),
                # Only set once actually paid — may differ from `amount` (the
                # originally scheduled figure) if it was overridden on mark-paid.
                'paid_amount': str(actual_paid) if p.status == 'paid' else None,
                'status': p.status,
                'paid_date': str(p.paid_date) if p.paid_date else None,
                'notes': p.notes,
                'period': {k: (str(v) if not isinstance(v, int) else v) for k, v in periods[p.id].items()} if p.id in periods else None,
                'is_balance': bool(p.balance_of_id),
                # Only a premature redemption keeps its own signed LOI and
                # the working behind it.
                'has_loi': bool(p.loi_document),
                'redemption': p.redemption_detail or None,
                'due_date': str(p.due_date),
                'term': term_of(terms, p.due_date),
                '_id': p.id,
            })
            total_due += p.amount_due
            if p.status == 'paid':
                total_paid += actual_paid
        # Same day: the investment first, interest before the principal it is paid
        # with, and an instalment before the balance it left.
        entries.sort(key=lambda e: (e['date'], {'investment': 0, 'interest': 1}.get(e['type'], 2),
                                    e.get('is_balance', False), e.get('_id') or 0))
        for e in entries:
            e.pop('_id', None)

        # The cards: what was invested, the interest on it, the two together, how much
        # of that has fallen due by today, what has gone out, and what is still to pay.
        today = date.today()
        principal = money_in(investor)    # the first term's money for an old renewed investment
        due_till_today = sum((p.amount_due or Decimal('0') for p in all_payouts if p.due_date <= today), Decimal('0'))
        summary = {
            'principal': str(principal),
            'interest': str(max(total_due - principal, Decimal('0'))),
            'total': str(total_due),
            'due_till_today': str(due_till_today),
            'paid_out': str(total_paid),
            'pending': str(total_due - total_paid),
            # Interest still to come: unpaid and not yet due.
            'upcoming_interest': str(sum((p.amount_due or Decimal('0') for p in all_payouts
                                          if p.payout_type == 'interest' and p.status == 'pending' and p.due_date > today), Decimal('0'))),
            # kept for older app builds
            'total_invested': str(principal),
            'total_payout_due': str(total_due),
            'total_paid': str(total_paid),
            'total_pending': str(total_due - total_paid),
        }
        # How each finished term ended — for the term tabs and the renewal LOI's history.
        outcomes = [{
            'return_pct': str(o['pct']), 'interest_payout': o['payout'],
            'renewed_with': str(o['next']), 'interest_earned': str(o['interest']),
            'interest_added': str(o['rolled']), 'interest_to_investor': str(max(o['interest'] - o['rolled'], Decimal('0'))),
            'principal_returned': str(o['returned']),
            'fresh_money': str(max(o['next'] - o['principal'] - o['rolled'], Decimal('0'))),
            'paid_at_renewal': ((investor.past_terms or [])[n] or {}).get('paid_at_renewal') or '',
        } for n, o in enumerate(past_terms(investor))]

        def _term_summary(t):
            mine = [p for p in all_payouts if term_of(terms, p.due_date) == t['no']]
            amt = lambda p: p.paid_amount if p.paid_amount is not None else p.amount_due
            due = sum((p.amount_due or Decimal('0') for p in mine), Decimal('0'))
            paid = sum((amt(p) or Decimal('0') for p in mine if p.status == 'paid'), Decimal('0'))
            back = sum((p.amount_due or Decimal('0') for p in mine if p.payout_type != 'interest'), Decimal('0'))
            interest = sum((p.amount_due or Decimal('0') for p in mine if p.payout_type == 'interest'), Decimal('0')) \
                + max(back - t['principal'], Decimal('0'))
            return {
                'principal': str(t['principal']), 'interest': str(interest), 'total': str(due),
                'due_till_today': str(sum((p.amount_due or Decimal('0') for p in mine if p.due_date <= today), Decimal('0'))),
                'paid_out': str(paid), 'pending': str(due - paid),
                'upcoming_interest': str(sum((p.amount_due or Decimal('0') for p in mine if p.payout_type == 'interest'
                                              and p.status == 'pending' and p.due_date > today), Decimal('0'))),
            }
        return Response({
            'investor': InvestorListSerializer(investor).data,
            'summary': summary,
            'entries': entries,
            'terms': [{'no': t['no'], 'label': t['label'], 'start': str(t['start']), 'maturity': str(t['maturity']),
                       'principal': str(t['principal']), 'current': t['current'], 'summary': _term_summary(t),
                       # its signed LOI can be opened (the running term's is the live one)
                       'has_loi': bool(investor.loi_document) if t['current'] else bool(((investor.past_terms or [])[t['no']] or {}).get('loi_document')),
                       **(outcomes[t['no']] if t['no'] < len(outcomes) and not t['current'] else {})}
                      for t in terms],
            'can_undo_renewal': bool(investor.renewal_count) and len(investor.past_terms or []) >= (investor.renewal_count or 0),
            # How many times the running term has been revised (its LOI reads "Renewal 2 · Revision 1").
            'current_term_revisions': term_revisions(investor),
        })


def term_revisions(investor):
    """Approved revisions of the term running now: those approved after its renewal was
    approved (all of them for an investment never renewed). revision_no counts revisions
    and renewals together, so it cannot tell "Renewal 2 revised once" from "R3"."""
    from activity.models import ActivityLog
    submitted, count, renewals = None, 0, 0
    for a in (ActivityLog.objects.filter(target_type='investor', target_id=str(investor.pk))
              .order_by('created_at', 'id').values_list('action', flat=True)):
        if a in ('revise', 'renew'):
            submitted = a
        elif a == 'approve' and submitted == 'revise':
            count += 1; submitted = None
        elif a == 'approve' and submitted == 'renew':
            count, renewals, submitted = 0, renewals + 1, None
        elif a == 'reject':
            submitted = None
    if not renewals and investor.renewal_count:
        # Renewed before the log knew of it — at best the revisions since.
        return max(count, 0)
    return count


def _months_between(start, end):
    """Whole months from start to end (a month counts once its day is reached)."""
    m = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        m -= 1
    return max(m, 0)


def _settlement(investor, on=None):
    """What closing this investment on `on` (today by default) works out to — the
    figures the Redeem / Payout / Renew dialogs show before anything is saved."""
    on = on or date.today()
    scheme = investor.scheme
    principal = investor.amount_invested or Decimal('0')
    months = _months_between(investor.investment_date, on) if investor.investment_date else 0
    lock = scheme.premature_redemption_lock_months or 0
    rate = scheme.premature_redemption_rate_pct_per_month or Decimal('0')
    paid = [p for p in investor.payouts.all() if p.status == 'paid']
    pending = sorted((p for p in investor.payouts.all() if p.status == 'pending'), key=lambda p: (p.due_date, p.payout_type == 'maturity', p.id))
    # Only this term's interest — a renewed investment's earlier terms were settled then.
    interest_paid = sum(((p.paid_amount if p.paid_amount is not None else p.amount_due) or Decimal('0')
                         for p in paid if p.payout_type == 'interest'
                         and (not investor.investment_date or p.due_date > investor.investment_date)), Decimal('0'))
    redemption_interest = (principal * Decimal(months) * rate / Decimal('100')).quantize(Decimal('0.01'))
    payable = max(principal + redemption_interest - interest_paid, Decimal('0')).quantize(Decimal('0.01'))
    if investor.status != 'active':
        reason = f'This investment is already {investor.get_status_display().lower()}.'
    elif not scheme.premature_redemption_allowed:
        reason = f'{scheme.name} does not allow redemption before maturity.'
    elif months < lock:
        reason = f'{scheme.name} is locked in for {lock} months — {months} done, free from {formatted_date(investor.investment_date + relativedelta(months=lock))}.'
    else:
        reason = ''
    return {
        'as_of': on, 'principal': principal, 'investment_date': investor.investment_date,
        'maturity_date': investor.maturity_date, 'is_matured': investor.is_matured(),
        'months_elapsed': months, 'lock_months': lock,
        'lock_until': investor.investment_date + relativedelta(months=lock) if investor.investment_date else None,
        'rate_per_month': rate, 'redemption_interest': redemption_interest,
        'interest_paid': interest_paid, 'payable': payable,
        'can_redeem': not reason, 'redeem_blocked_reason': reason,
        'pending': _with_periods(PayoutSerializer(pending, many=True).data, [investor]),
        'pending_total': sum((p.amount_due or Decimal('0') for p in pending), Decimal('0')),
    }


def formatted_date(d):
    return d.strftime('%d/%m/%Y') if d else ''


def _with_periods(rows, investors):
    """Add each interest payout's working (period, days, per-day interest, rate,
    FY quarter) to serialized payout rows, as row['period']."""
    ids = {r['investor'] for r in rows if r.get('payout_type') == 'interest'}
    if not ids:
        return rows
    by_inv = {}
    for p in Payout.objects.filter(investor_id__in=ids, payout_type='interest'):
        by_inv.setdefault(p.investor_id, []).append(p)
    periods = {}
    for inv in investors:
        if inv.pk in ids:
            periods.update(interest_periods(inv, by_inv.get(inv.pk, [])))
    for r in rows:
        if r['id'] in periods:
            per = periods[r['id']]
            r['period'] = {k: (str(v) if not isinstance(v, int) else v) for k, v in per.items()}
    return rows


_HISTORY_SKIP = {'draft', 'loi-no', 'upload-loi', 'updated'}


def _day_start(d):
    """A paid date as a moment, so it sorts among the logged events."""
    return timezone.make_aware(datetime.combine(d, datetime.min.time()))


class InvestorHistoryView(APIView):
    """GET one investment's life from the start — added, approved, every revision
    and renewal (submitted / approved / rejected), payouts paid and undone,
    referral rewards, redemption and closing — newest first.

    Built from the activity log (who did what, when) plus the payout and reward
    rows themselves, so payments made inside a Payout / Redeem dialog, and
    records older than the log, still appear."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        from activity.models import ActivityLog
        investor = _investor_visibility_qs(request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        events = []

        def add(at, kind, title, detail='', by='', amount=None):
            events.append({'at': at, 'kind': kind, 'title': title, 'detail': detail, 'by': by,
                           'amount': str(amount) if amount is not None else None})

        def detail_of(summary):
            # "Approved investor — NAME · LOI X · Field: a → b; …" → the part after the label.
            parts = (summary or '').split(' · ')
            return ' · '.join(parts[2:]) if len(parts) > 2 else ''

        logs = list(ActivityLog.objects.filter(target_type='investor', target_id=str(investor.pk))
                    .order_by('created_at', 'id'))
        submitted = 'new'       # what the next approve / reject answers
        revisions = renewals = 0
        seen = set()
        for r in logs:
            a = r.action
            if a in _HISTORY_SKIP:
                continue
            by = r.actor_name or ''
            if a == 'created':
                add(r.created_at, 'added', 'Investment added', detail_of(r.summary), by, investor.amount_invested)
                seen.add('added')
            elif a == 'revise':
                revisions += 1; submitted = 'revise'
                add(r.created_at, 'revise', f'Revision R{revisions} submitted for approval', '', by)
            elif a == 'renew':
                renewals += 1; submitted = 'renew'
                add(r.created_at, 'renew', f'Renewal #{renewals} submitted for approval', '', by)
            elif a in ('approve', 'reject'):
                verb = 'approved' if a == 'approve' else 'rejected'
                title = {'new': f'Investment {verb}', 'revise': f'Revision R{revisions} {verb}',
                         'renew': f'Renewal #{renewals} {verb}'}[submitted]
                add(r.created_at, a if submitted == 'new' else f'{submitted}-{a}', title, detail_of(r.summary), by)
                if submitted == 'new' and a == 'approve':
                    seen.add('approved')
            elif a == 'redeem':
                add(r.created_at, 'redeem', 'Redeemed before maturity', detail_of(r.summary), by)
            elif a == 'mature-payout':
                add(r.created_at, 'closed', 'Paid out at maturity — investment closed', detail_of(r.summary), by)
            else:
                add(r.created_at, a, (r.summary or a).split(' — ')[0], detail_of(r.summary), by)

        # Records older than the activity log still get their start.
        if 'added' not in seen:
            add(investor.created_at, 'added', 'Investment added', '',
                investor.added_by.name if investor.added_by else '', investor.amount_invested)
        if 'approved' not in seen and investor.approved_at:
            add(investor.approved_at, 'approve', 'Investment approved', '',
                investor.approved_by.name if investor.approved_by else '')

        # Payouts: every paid one (whichever screen paid it), and every undo.
        payouts = list(investor.payouts.select_related('paid_by').all())
        periods = interest_periods(investor, payouts)
        labels = {'interest': 'Interest', 'maturity': 'Principal', 'premature_redemption': 'Redemption'}
        for p in payouts:
            if p.status == 'paid' and p.paid_date:
                paid = p.paid_amount if p.paid_amount is not None else p.amount_due
                bits = [f'due {formatted_date(p.due_date)}']
                per = periods.get(p.id)
                if per:
                    bits.insert(0, f"{per['label']} · {formatted_date(per['from'])}–{formatted_date(per['to'])} · {per['days']} days × ₹{per['per_day']:,}/day")
                if paid != p.amount_due:
                    bits.append(f'target ₹{p.amount_due:,.0f}')
                if p.notes:
                    bits.append(p.notes)
                add(_day_start(p.paid_date), 'paid', f'{labels.get(p.payout_type, p.payout_type)}{" balance" if p.balance_of_id else ""} paid', ' · '.join(bits),
                    p.paid_by.name if p.paid_by else '', paid)
        pay_logs = ActivityLog.objects.filter(target_type='payout', action__in=['mark-unpaid', 'delete-payment', 'edit-payment'],
                                              target_id__in=[str(p.pk) for p in payouts])
        for r in pay_logs:
            # "Edited payout payment — NAME · ₹a on d → ₹b on e": the part after the name.
            what = (r.summary or '').split(' — ', 1)[-1].split(' · ', 1)[-1] if ' — ' in (r.summary or '') else ''
            if r.action == 'edit-payment':
                add(r.created_at, 'edit', 'Payment edited', what, r.actor_name or '')
            else:
                add(r.created_at, 'undo', 'Payment deleted — back to pending', what if r.action == 'delete-payment' else detail_of(r.summary), r.actor_name or '')

        for rw in investor.referral_rewards.select_related('paid_by').all():
            who = f'{rw.reference_name or "Referrer"}{f" ({rw.reference_phone})" if rw.reference_phone else ""}'
            add(rw.created_at, 'reward', f'Referral reward earned — {who}', f'{rw.pct}% of the investment', '', rw.amount)
            if rw.status == 'paid' and rw.paid_date:
                add(_day_start(rw.paid_date), 'reward-paid', f'Referral reward paid — {who}', rw.notes or '',
                    rw.paid_by.name if rw.paid_by else '', rw.paid_amount if rw.paid_amount is not None else rw.amount)

        events.sort(key=lambda e: e['at'], reverse=True)
        paid_total = sum(((p.paid_amount if p.paid_amount is not None else p.amount_due) or Decimal('0')
                          for p in payouts if p.status == 'paid'), Decimal('0'))
        return Response({
            'investor': investor.pk,
            'renewal_count': investor.renewal_count,
            'revision_count': max(revisions, (investor.revision_no or 0) - (investor.renewal_count or 0)),
            'status': investor.status,
            'term': {'from': investor.investment_date, 'to': investor.maturity_date},
            'paid_total': paid_total,
            'events': events,
        })


class InvestorSettlementView(APIView):
    """GET the figures for closing an investment (Redeem / Payout / Renew dialogs).
    `?on=YYYY-MM-DD` works a redemption out to another date."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = _investor_visibility_qs(request).select_related('scheme').prefetch_related('payouts').filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        on = _parse_date(request.query_params.get('on')) or date.today()
        return Response(_settlement(investor, on))


def _rebalance(payout):
    """Keep what is still owed on an instalment right after it is paid or its
    payment edited. The instalment's whole target is its own amount_due plus any
    balance still pending from it; when less than that was paid, the instalment
    is settled for what was paid and the rest stays due as a pending balance
    payout (same type and due date) — never silently dropped. Paying the target
    or more clears the pending balance."""
    if not isinstance(payout, Payout) or payout.status != 'paid':
        return
    pending_kids = list(payout.balances.filter(status='pending'))
    target = (payout.amount_due or Decimal('0')) + sum((k.amount_due or Decimal('0') for k in pending_kids), Decimal('0'))
    paid = payout.paid_amount if payout.paid_amount is not None else payout.amount_due
    if paid is not None and target - paid >= Decimal('1'):
        rest = (target - paid).quantize(Decimal('0.01'))
        payout.amount_due = paid
        payout.save(update_fields=['amount_due', 'updated_at'])
        note = f'Balance of the {formatted_date(payout.due_date)} instalment — ₹{paid:,.2f} of ₹{target:,.2f} paid on {formatted_date(payout.paid_date)}'
        if pending_kids:
            keep, extra = pending_kids[0], pending_kids[1:]
            keep.amount_due, keep.notes = rest, note
            keep.save(update_fields=['amount_due', 'notes', 'updated_at'])
            for k in extra:
                k.delete()
        else:
            Payout.objects.create(investor=payout.investor, payout_type=payout.payout_type, due_date=payout.due_date,
                                  amount_due=rest, balance_of=payout, notes=note)
    else:
        if pending_kids:
            payout.amount_due = target
            payout.save(update_fields=['amount_due', 'updated_at'])
            for k in pending_kids:
                k.delete()


def _pay_payout(payout, amount, paid_on, notes, user):
    payout.status = 'paid'
    payout.paid_amount = amount if amount is not None else payout.amount_due
    payout.paid_date = paid_on
    payout.notes = notes or payout.notes or ''
    payout.paid_by = user
    payout.save(update_fields=['status', 'paid_amount', 'paid_date', 'notes', 'paid_by', 'updated_at'])
    _rebalance(payout)
    _close_if_paid_out(payout.investor)


def _close_if_paid_out(investor):
    """A matured investment whose principal has been paid in full from Payouts is
    closed — what the Payout button on Investors used to do — and opens again if
    that payment is undone or leaves a balance due."""
    if investor.status not in ('active', 'matured', 'redeemed') or not investor.maturity_date:
        return
    final = [p for p in investor.payouts.all() if p.payout_type == 'maturity' and p.due_date >= investor.maturity_date]
    settled = bool(final) and investor.maturity_date <= date.today() and all(p.status == 'paid' for p in final)
    want = 'redeemed' if settled else ('active' if investor.status == 'redeemed' else investor.status)
    if want != investor.status:
        investor.status = want
        investor.save(update_fields=['status'])


def _money(value, label):
    try:
        v = Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'Invalid {label}.')
    if v < 0:
        raise ValueError(f'{label.capitalize()} cannot be negative.')
    return v


def _past_date(value, label, default=None):
    if not value:
        return default or date.today()
    d = _parse_date(value)
    if not d:
        raise ValueError(f'Invalid {label}.')
    if d > date.today():
        raise ValueError(f'The {label} cannot be in the future.')
    return d


def _inr(amount):
    """A rupee figure in Indian grouping — 1000000 -> 10,00,000.

    Written out because these strings end up in notes and log lines that a
    person reads; "Redeemed 1000000.00" is a number nobody checks.
    """
    n = int(Decimal(amount or 0).quantize(Decimal('1')))
    sign, n = ('-' if n < 0 else ''), abs(n)
    text = str(n)
    if len(text) > 3:
        head, tail = text[:-3], text[-3:]
        # Indian grouping: the last three digits, then pairs.
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        text = ','.join(parts + [tail])
    return f'{sign}\u20b9{text}'


def _partial_settlement(investor, redeem_amount, on=None):
    """What redeeming `redeem_amount` of this investment on `on` works out to.

    The withdrawn money is penalised and the rest is not. The part taken out
    earns the scheme's premature rate for the months it actually sat invested
    (redeem_amount x rate x months); the part left behind keeps the scheme's
    normal return and its original maturity date, and only its payout schedule
    is resized to the smaller principal.

    interest_paid_share is reported but NOT deducted. The withdrawn half may
    already have been paid interest at the normal rate for those months, which
    is more than the premature rate allows — that difference is shown on the
    dialog and the LOI so whoever signs can see it, but the payable figure is
    the gross one Chinmay specified: principal taken out, plus premature
    interest on it. (Full redemption, the older path, does deduct it — the two
    differ on purpose, not by accident.)
    """
    on = on or date.today()
    scheme = investor.scheme
    principal = investor.amount_invested or Decimal('0')
    redeem_amount = Decimal(redeem_amount or 0).quantize(Decimal('0.01'))
    months = _months_between(investor.investment_date, on) if investor.investment_date else 0
    lock = scheme.premature_redemption_lock_months or 0
    rate = scheme.premature_redemption_rate_pct_per_month or Decimal('0')

    # Priced by the day, not by whole months. A redemption on the 19th had its
    # last eighteen days thrown away when the interest was months x rate; the
    # money sat invested for them, so it earns for them.
    #
    # The scheme states a per-MONTH rate, so it is annualised and spread over
    # 365 — the same actual-day-count basis the payout schedule already uses
    # (see services.generate_payout_schedule), rather than a notional 30-day
    # month that would disagree with it.
    days_held = (on - investor.investment_date).days if investor.investment_date else 0
    days_held = max(days_held, 0)
    daily_rate_pct = rate * Decimal('12') / Decimal('365')
    # Per-day rupees on the amount coming out, for the working to show.
    daily_interest = (redeem_amount * daily_rate_pct / Decimal('100')).quantize(Decimal('0.01'))
    premature_interest = (redeem_amount * daily_rate_pct * Decimal(days_held)
                          / Decimal('100')).quantize(Decimal('0.01'))
    payable = (redeem_amount + premature_interest).quantize(Decimal('0.01'))
    remaining = (principal - redeem_amount).quantize(Decimal('0.01'))

    # Interest already handed over on the share being withdrawn, for the
    # signer's benefit — see the docstring on why it is not subtracted.
    paid_this_term = sum(
        ((p.paid_amount if p.paid_amount is not None else p.amount_due) or Decimal('0')
         for p in investor.payouts.all()
         if p.status == 'paid' and p.payout_type == 'interest'
         and (not investor.investment_date or p.due_date > investor.investment_date)),
        Decimal('0'))
    share = (redeem_amount / principal) if principal else Decimal('0')
    interest_paid_share = (paid_this_term * share).quantize(Decimal('0.01'))

    if investor.status != 'active':
        reason = f'This investment is already {investor.get_status_display().lower()}.'
    elif not scheme.premature_redemption_allowed:
        reason = f'{scheme.name} does not allow redemption before maturity.'
    elif months < lock:
        reason = (f'{scheme.name} is locked in for {lock} months — {months} done, free from '
                  f'{formatted_date(investor.investment_date + relativedelta(months=lock))}.')
    elif redeem_amount <= 0:
        reason = 'Enter how much is being redeemed.'
    elif redeem_amount > principal:
        reason = f'Cannot redeem more than the {principal} invested.'
    elif remaining > 0 and remaining < (scheme.min_ticket_size or 0):
        # Leaving less than the scheme's floor behind is not a smaller
        # investment, it is one the scheme would never have accepted.
        reason = (f'Redeeming this much leaves {remaining}, below {scheme.name}\'s minimum of '
                  f'{scheme.min_ticket_size}. Redeem less, or redeem it all.')
    else:
        reason = ''

    return {
        'as_of': on,
        'principal': principal,
        'redeem_amount': redeem_amount,
        'remaining_principal': remaining,
        'is_full': remaining <= 0,
        'months_elapsed': months,
        'days_held': days_held,
        'daily_interest': daily_interest,
        'daily_rate_pct': daily_rate_pct.quantize(Decimal('0.000001')),
        'lock_months': lock,
        'rate_per_month': rate,
        'premature_interest': premature_interest,
        'payable': payable,
        'interest_paid_share': interest_paid_share,
        'normal_return_pct': investor.total_return_pct,
        'maturity_date': investor.maturity_date,
        'can_redeem': not reason,
        'blocked_reason': reason,
    }


def _apply_redemption(investor, actor):
    """Put an approved partial redemption into effect.

    The figures come from pending_revision, not from recomputing: they are what
    the signed LOI says, and a fresh calculation on the approval date would
    quietly disagree with the paper someone signed.

    What changes: a Premature Redemption payout for the money going out, the
    principal reduced by the part withdrawn, and the remaining schedule resized
    for what is left. The maturity date and the return % do not move — the rest
    of the investment carries on exactly as agreed.
    """
    pr = investor.pending_revision or {}
    dec = lambda k: Decimal(str(pr.get(k) or '0'))
    try:
        on = date.fromisoformat(str(pr.get('redemption_date')))
    except (ValueError, TypeError):
        on = date.today()
    redeemed = dec('redeem_amount')
    remaining = dec('remaining_principal')

    investor.redeemed_principal = (investor.redeemed_principal or Decimal('0')) + redeemed
    investor.redemption_count = (investor.redemption_count or 0) + 1
    investor.amount_invested = remaining
    fields = ['redeemed_principal', 'redemption_count', 'amount_invested', 'updated_at']

    # Clear the schedule BEFORE writing this redemption's payout: the
    # instalments still owed were sized for the larger principal and are
    # rebuilt below, and a redemption payout created first would be pending too
    # and swept away with it. Paid instalments are left alone — that money has
    # gone out.
    #
    # Earlier redemptions are excluded explicitly. They are pending because the
    # money has not been handed over yet, NOT because they are a stale
    # schedule — deleting them wiped both the amount owed and the signed LOI
    # kept against it, so a second redemption silently erased the first.
    (investor.payouts
     .filter(status='pending')
     .exclude(payout_type='premature_redemption')
     .delete())
    Payout.objects.create(
        investor=investor, payout_type='premature_redemption', due_date=on,
        amount_due=dec('payable'),
        notes=(pr.get('notes') or '')
              or f"Redeemed {_inr(redeemed)} after {pr.get('days_held')} days",
        # The LOI signed for this redemption, captured before the swap below
        # hands it to the investor record — where the next revision or renewal
        # would overwrite it.
        loi_document=(investor.pending_loi_document.name
                      if investor.pending_loi_document else None),
        # Kept so this redemption can be shown in full later — on the next
        # redemption's LOI, and anywhere the history is read back.
        redemption_detail={k: pr.get(k) for k in (
            'redemption_date', 'redeem_amount', 'premature_interest', 'payable',
            'remaining_principal', 'days_held', 'daily_interest', 'rate_per_month',
            'interest_paid_share')},
    )
    if remaining > 0:
        generate_payout_schedule(investor, custom_entries=pr.get('payout_schedule'))
    else:
        # Nothing left invested: the investment is closed, not merely smaller.
        investor.status = 'premature_redeemed'
        fields.append('status')

    # The signed redemption LOI becomes the investment's LOI, and the staged
    # fields are cleared — the same close-out the revision branch does, which
    # this path deliberately steps around.
    if investor.loi_document:
        try:
            investor.loi_document.delete(save=False)
        except Exception:
            pass
    if investor.pending_loi_document:
        investor.loi_document.name = investor.pending_loi_document.name
        investor.pending_loi_document = None
    investor.pending_revision = None
    investor.pending_revision_type = 'revise'
    investor.approval_status = 'approved'
    investor.approved_by, investor.approved_at = actor, timezone.now()
    fields += ['loi_document', 'pending_loi_document', 'pending_revision',
               'pending_revision_type', 'approval_status', 'approved_by', 'approved_at']
    investor.save(update_fields=fields)


class InvestorRedeemQuoteView(APIView):
    """The working for a proposed partial redemption, before anything is saved —
    what the Redeem dialog and the LOI both print."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = _company_filtered(
            Investor.objects.select_related('scheme'), request
        ).prefetch_related('payouts').filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        try:
            on = _past_date(request.query_params.get('redemption_date'), 'redemption date')
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        amount = request.query_params.get('amount')
        try:
            amount = investor.amount_invested if amount in (None, '') else _money(amount, 'amount')
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_partial_settlement(investor, amount, on))


class InvestorRedeemRequestView(APIView):
    """Propose a partial (or full) premature redemption + a newly signed LOI.

    Redeeming money out of a live investment changes what is owed, so it goes
    through the same staged approval the revision and renewal flows use rather
    than taking effect on the spot: held in pending_revision /
    pending_loi_document with pending_revision_type='redeem' until
    InvestorActionView approves it. A rejected redemption leaves the
    investment exactly as it was.

    This is the partial-aware path. InvestorRedeemView above is the older
    all-or-nothing one, kept for the full-redemption flow already in use.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not club_can(request.user, 'club.investor.manage'):
            return _no_permission()
        investor = _investor_visibility_qs(request).prefetch_related('payouts').filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if investor.approval_status != 'approved':
            return Response({'detail': 'Only an approved investor can be redeemed.'},
                            status=status.HTTP_400_BAD_REQUEST)
        loi = request.data.get('loi_file')
        if not isinstance(loi, dict) or not loi.get('data'):
            return Response({'detail': 'loi_file is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            on = _past_date(request.data.get('redemption_date'), 'redemption date')
            amount = _money(request.data.get('amount'), 'amount')
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if investor.investment_date and on < investor.investment_date:
            return Response({'detail': 'The redemption date is before the investment date.'},
                            status=status.HTTP_400_BAD_REQUEST)

        st = _partial_settlement(investor, amount, on)
        if not st['can_redeem']:
            return Response({'detail': st['blocked_reason']}, status=status.HTTP_400_BAD_REQUEST)

        # The whole working is stored, not just the amount: these are the
        # figures the signed LOI shows, and recomputing them at approval time
        # against a later date would quietly disagree with the paper.
        pending = {
            'redemption_date': str(on),
            'redeem_amount': str(st['redeem_amount']),
            'premature_interest': str(st['premature_interest']),
            'payable': str(st['payable']),
            'remaining_principal': str(st['remaining_principal']),
            'months_elapsed': st['months_elapsed'],
            'days_held': st['days_held'],
            'daily_interest': str(st['daily_interest']),
            'rate_per_month': str(st['rate_per_month']),
            'interest_paid_share': str(st['interest_paid_share']),
            'notes': (request.data.get('notes') or '').strip(),
        }
        schedule = request.data.get('payout_schedule')
        if isinstance(schedule, list):
            pending['payout_schedule'] = schedule

        investor.pending_revision = pending
        investor.pending_revision_type = 'redeem'
        investor.revision_no += 1
        investor.approval_status = 'pending'
        investor.revision_draft = None
        import base64
        from django.core.files.base import ContentFile
        try:
            investor.pending_loi_document.save(
                _investor_pending_loi_path(investor),
                ContentFile(base64.b64decode(loi['data'])), save=False,
            )
        except Exception:
            # Imported here as the rest of this module does.
            import logging
            logging.getLogger(__name__).exception('Investor redemption LOI save failed for %s', investor.id)
            return Response({'detail': 'Could not save the signed LOI.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        investor.save(update_fields=[
            'pending_revision', 'pending_revision_type', 'revision_no', 'approval_status',
            'pending_loi_document', 'revision_draft', 'updated_at'])
        _notify_investor_approvers(investor.company, investor, request.user)
        # The Log should say what was asked for and on whose investment, not
        # just "updated investor".
        from activity.recorder import note
        note(request,
             f'Redemption of {_inr(st["redeem_amount"])} from {investor.name}'
             f'{" (" + investor.loi_no + ")" if investor.loi_no else ""} sent for approval — '
             f'{st["days_held"]} days at {st["rate_per_month"]}%/month, '
             f'{_inr(st["payable"])} payable, {_inr(st["remaining_principal"])} left invested',
             action='update', target_type='Investor', target_id=investor.pk, module='Club 1000')
        return Response(InvestorListSerializer(investor).data, status=status.HTTP_201_CREATED)


class InvestorRedeemView(APIView):
    """Redeem before maturity. The dialog shows the working (principal + redemption
    interest − interest already paid) and sends:
      redemption_date  the day it is redeemed (today by default; never later)
      amount           what is payable (the working's figure unless changed)
      notes            why / remarks
      pay_now          {paid_date, amount, notes} — paid on the spot (optional)
    Every still-pending payout is replaced by one Premature Redemption payout for
    the payable amount, so nothing is owed twice."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not club_can(request.user, 'club.investor.manage'):
            return _no_permission()
        investor = _company_filtered(Investor.objects.select_related('scheme'), request).prefetch_related('payouts').filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        try:
            on = _past_date(request.data.get('redemption_date'), 'redemption date')
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if investor.investment_date and on < investor.investment_date:
            return Response({'detail': 'The redemption date is before the investment date.'}, status=status.HTTP_400_BAD_REQUEST)
        st = _settlement(investor, on)
        if not st['can_redeem']:
            return Response({'detail': st['redeem_blocked_reason']}, status=status.HTTP_400_BAD_REQUEST)
        pay_now = request.data.get('pay_now') if isinstance(request.data.get('pay_now'), dict) else None
        try:
            amount = st['payable'] if request.data.get('amount') in (None, '') else _money(request.data.get('amount'), 'amount')
            if pay_now:
                paid_on = _past_date(pay_now.get('paid_date'), 'paid date')
                paid_amt = amount if pay_now.get('amount') in (None, '') else _money(pay_now.get('amount'), 'paid amount')
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        notes = (request.data.get('notes') or '').strip()
        with transaction.atomic():
            investor.payouts.filter(status='pending').delete()
            payout = Payout.objects.create(
                investor=investor, payout_type='premature_redemption', due_date=on, amount_due=amount,
                notes=notes or f"Redeemed after {st['months_elapsed']} months",
            )
            if pay_now:
                _pay_payout(payout, paid_amt, paid_on, (pay_now.get('notes') or '').strip(), request.user)
            investor.status = 'premature_redeemed'
            investor.save(update_fields=['status'])
        return Response({
            'investor': InvestorListSerializer(investor).data,
            'payout': PayoutSerializer(payout).data,
        })


class InvestorMaturityPayoutView(APIView):
    """A matured investor who doesn't want to renew takes their Payout instead
    — an instant manager action (no approval queue, mirrors the immediacy of
    InvestorRedeemView) that just closes out the investment. The already-
    scheduled maturity Payout row is marked paid separately, via the Payouts
    screen — this view only flips the investor's own status."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        investor = _company_filtered(Investor.objects.select_related('scheme'), request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not investor.is_matured():
            return Response({'detail': 'Investor has not matured yet.'}, status=status.HTTP_400_BAD_REQUEST)
        # The Payout dialog lists every pending payout (principal and any unpaid
        # interest) and sends the ones paid now: [{id, amount, paid_date, notes}].
        # Those not sent stay pending on Payouts.
        payments = request.data.get('payments') or []
        if not isinstance(payments, list):
            return Response({'detail': 'payments must be a list.'}, status=status.HTTP_400_BAD_REQUEST)
        pending = {p.id: p for p in investor.payouts.filter(status='pending')}
        plan = []
        try:
            for item in payments:
                p = pending.get(int(item.get('id') or 0)) if isinstance(item, dict) else None
                if p is None:
                    raise ValueError('A payout in the list is not pending for this investor.')
                amt = p.amount_due if item.get('amount') in (None, '') else _money(item.get('amount'), 'amount')
                plan.append((p, amt, _past_date(item.get('paid_date'), 'paid date'), (item.get('notes') or '').strip()))
        except (ValueError, TypeError) as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            for p, amt, paid_on, notes in plan:
                _pay_payout(p, amt, paid_on, notes, request.user)
            investor.status = 'redeemed'
            investor.save(update_fields=['status'])
        return Response(InvestorListSerializer(investor).data)


class PayoutListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(Payout.objects.select_related('investor', 'investor__scheme'), request, field='investor__company')
        _scoped = scope_investors(qs, request.user, 'investor__')
        if _scoped is not None:
            qs = _scoped
        elif not is_club1000_manager(request.user):
            qs = qs.filter(investor__added_by=request.user)
        if ai_capture(request, qs=qs):
            return Response({})
        p = request.query_params
        if p.get('status'):
            qs = qs.filter(status=p['status'])
        if p.get('payout_type'):
            qs = qs.filter(payout_type=p['payout_type'])
        if p.get('scheme_id'):
            qs = qs.filter(investor__scheme_id=p['scheme_id'])
        if p.get('investor'):
            qs = qs.filter(investor_id=p['investor'])
        # Dated by due date — the only date this ledger is read by. A paid row's
        # paid_date is on the row for reference, not something to filter a
        # "what's owed in March" question by.
        d_from = _parse_date(p.get('date_from'))
        d_to = _parse_date(p.get('date_to'))
        if d_from:
            qs = qs.filter(due_date__gte=d_from)
        if d_to:
            qs = qs.filter(due_date__lte=d_to)
        # The two questions this screen actually exists to answer, neither of
        # which any stored column holds: what has slipped, and what lands next.
        # Both mean pending — a payout already made is not late, however old.
        bucket = p.get('bucket')
        today = date.today()
        if bucket == 'overdue':
            qs = qs.filter(status='pending', due_date__lt=today)
        elif bucket == 'due_soon':
            qs = qs.filter(status='pending', due_date__gte=today,
                           due_date__lte=today + timedelta(days=30))
        if p.get('facets') in ('1', 'true'):
            # Only the values these rows hold, so no filter can empty the list
            # for no reason — same contract as the investor list's ?facets=1.
            return Response({
                'statuses': _distinct(qs, 'status'),
                'types': _distinct(qs, 'payout_type'),
                'scheme_ids': _distinct(qs, 'investor__scheme_id'),
            })
        search = p.get('search', '').strip()
        if search:
            # investor name/phone are encrypted, so SQL cannot match them —
            # a full ten-digit number goes through the blind index, anything
            # else is matched in Python over the already-scoped rows.
            digits = ''.join(c for c in search if c.isdigit())
            if len(digits) >= 10 and not any(c.isalpha() for c in search):
                qs = qs.filter(investor__phone_key=phone_blind_index(digits))
            else:
                needle = search.lower()
                hits = [pk for pk, nm, ph in qs.values_list('id', 'investor__name', 'investor__phone')
                        if needle in (nm or '').lower() or needle in (ph or '').lower()]
                qs = qs.filter(id__in=hits)
        rows = PayoutSerializer(qs, many=True).data
        inv_ids = {r['investor'] for r in rows}
        return Response(_with_periods(rows, Investor.objects.filter(pk__in=inv_ids)))


class PayoutMarkPaidView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not club_can(request.user, 'club.payout.mark_paid'):
            return _no_permission()
        payout = _company_filtered(
            Payout.objects.select_related('investor'), request, field='investor__company'
        ).filter(pk=pk).first()
        if not payout:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        # Optional override of the actually-disbursed amount (rounding, a partial
        # payment, a negotiated adjustment) — falls back to the scheduled amount.
        amount = request.data.get('amount')
        if amount not in (None, ''):
            try:
                payout.paid_amount = Decimal(str(amount))
            except (InvalidOperation, ValueError, TypeError):
                return Response({'detail': 'Invalid amount.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            payout.paid_amount = payout.amount_due
        notes = request.data.get('notes')
        if notes is not None:
            payout.notes = notes
        # The day the money actually went out — the Pay dialog sends it per payout
        # (a transfer made yesterday is entered today). Defaults to today; never later.
        paid_on = date.today()
        if request.data.get('paid_date'):
            paid_on = _parse_date(request.data.get('paid_date'))
            if not paid_on:
                return Response({'detail': 'Invalid paid date.'}, status=status.HTTP_400_BAD_REQUEST)
            if paid_on > date.today():
                return Response({'detail': 'The paid date cannot be in the future.'}, status=status.HTTP_400_BAD_REQUEST)
        payout.status = 'paid'
        payout.paid_date = paid_on
        payout.paid_by = request.user
        with transaction.atomic():
            payout.save(update_fields=['status', 'paid_amount', 'notes', 'paid_date', 'paid_by', 'updated_at'])
            _rebalance(payout)   # paid short → the rest stays due
            _close_if_paid_out(payout.investor)
        return Response(PayoutSerializer(payout).data)


def _undo_paid(obj):
    """Back to pending — a payment marked by mistake. What was paid, when, by whom
    and the remarks are cleared (the activity log keeps the before/after)."""
    if isinstance(obj, Payout):
        # The balance it left pending folds back in: the instalment is whole again.
        kids = list(obj.balances.filter(status='pending'))
        if kids:
            obj.amount_due = (obj.amount_due or Decimal('0')) + sum((k.amount_due or Decimal('0') for k in kids), Decimal('0'))
            for k in kids:
                k.delete()
    obj.status = 'pending'
    obj.paid_date = None
    obj.paid_amount = None
    obj.paid_by = None
    obj.notes = ''
    obj.save(update_fields=['status', 'paid_date', 'paid_amount', 'paid_by', 'notes', 'amount_due', 'updated_at']
             if isinstance(obj, Payout) else ['status', 'paid_date', 'paid_amount', 'paid_by', 'notes', 'updated_at'])
    if isinstance(obj, Payout):
        _close_if_paid_out(obj.investor)


def _edit_payment(obj, data, user):
    """Change what was paid, when, and the remarks on a payment already recorded."""
    if obj.status != 'paid':
        raise ValueError('This one is not marked paid.')
    if 'amount' in data and data.get('amount') not in (None, ''):
        obj.paid_amount = _money(data.get('amount'), 'amount')
    if data.get('paid_date'):
        obj.paid_date = _past_date(data.get('paid_date'), 'paid date')
    if 'notes' in data:
        obj.notes = (data.get('notes') or '').strip()
    obj.save(update_fields=['paid_amount', 'paid_date', 'notes', 'updated_at'])
    _rebalance(obj)   # a payment edited down keeps the rest due; edited up clears it


class _PaymentView(APIView):
    """A recorded payment, like an AR receipt: PATCH edits it (amount, date,
    remarks), DELETE removes it — the payout / reward goes back to pending.
    Managers who may mark paid."""
    permission_classes = [IsAuthenticated]
    model = None
    serializer = None
    noun = 'payment'

    def _obj(self, request, pk):
        if not (club_can(request.user, 'club.payout.mark_paid') and is_club1000_manager(request.user)):
            return None, _no_permission()
        obj = _company_filtered(self.model.objects.select_related('investor'), request, field='investor__company').filter(pk=pk).first()
        if not obj:
            return None, Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if obj.status != 'paid':
            return None, Response({'detail': f'This {self.noun} is not marked paid.'}, status=status.HTTP_400_BAD_REQUEST)
        return obj, None

    def patch(self, request, pk):
        obj, err = self._obj(request, pk)
        if err:
            return err
        from activity.recorder import note
        before = (obj.paid_amount, obj.paid_date, obj.notes)
        try:
            _edit_payment(obj, request.data, request.user)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(obj, Payout):
            _close_if_paid_out(obj.investor)
        note(request, f'Edited {self.noun} payment — {obj.investor.name} · ₹{before[0] or 0:,.0f} on {formatted_date(before[1])} → '
                      f'₹{obj.paid_amount or 0:,.0f} on {formatted_date(obj.paid_date)}', action='edit-payment',
             target_type=self.noun if self.noun != 'reward' else 'referral-reward', target_id=str(pk), module='Club 1000')
        return Response(self.serializer(obj).data)

    def delete(self, request, pk):
        obj, err = self._obj(request, pk)
        if err:
            return err
        from activity.recorder import note
        was = f'₹{(obj.paid_amount if obj.paid_amount is not None else 0):,.0f} on {formatted_date(obj.paid_date)}'
        _undo_paid(obj)
        note(request, f'Deleted {self.noun} payment — {obj.investor.name} · {was} · back to pending', action='delete-payment',
             target_type=self.noun if self.noun != 'reward' else 'referral-reward', target_id=str(pk), module='Club 1000')
        return Response(self.serializer(obj).data)


class PayoutMarkUnpaidView(APIView):
    """Undo a payout marked paid by mistake — managers who may mark paid."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not (club_can(request.user, 'club.payout.mark_paid') and is_club1000_manager(request.user)):
            return _no_permission()
        payout = _company_filtered(
            Payout.objects.select_related('investor'), request, field='investor__company'
        ).filter(pk=pk).first()
        if not payout:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if payout.status != 'paid':
            return Response({'detail': 'This payout is not marked paid.'}, status=status.HTTP_400_BAD_REQUEST)
        _undo_paid(payout)
        return Response(PayoutSerializer(payout).data)


class ReferralRewardListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(ReferralReward.objects.select_related('investor', 'investor__lead'), request, field='investor__company')
        if not is_club1000_manager(request.user):
            # A referral reward belongs to whoever owns the CLOSURE — i.e. the
            # Lead that converted into this investment — not whoever happened
            # to click "Add Investor". Falls back to added_by only when there's
            # no lead at all (or the lead has no assignee) to attribute to.
            no_lead_owner = Q(investor__lead__isnull=True) | Q(investor__lead__assigned_to__isnull=True)
            qs = qs.filter(
                Q(investor__lead__assigned_to=request.user) | (no_lead_owner & Q(investor__added_by=request.user))
            )
        if ai_capture(request, qs=qs):          # Ask Nexora: the rewards this person's screen lists
            return Response({})
        p = request.query_params
        if p.get('status'):
            qs = qs.filter(status=p['status'])
        if p.get('scheme_id'):
            qs = qs.filter(investor__scheme_id=p['scheme_id'])
        if p.get('investor'):
            qs = qs.filter(investor_id=p['investor'])
        # The rate a reward was locked in at says what earned it: 0.5% a new
        # investment, 0.25% a renewal at maturity. Nothing else records which.
        if p.get('pct'):
            try:
                qs = qs.filter(pct=Decimal(str(p['pct'])))
            except (InvalidOperation, ValueError):
                pass
        # Dated by when it was earned — the column the table shows.
        d_from = _parse_date(p.get('date_from'))
        d_to = _parse_date(p.get('date_to'))
        if d_from:
            qs = qs.filter(created_at__date__gte=d_from)
        if d_to:
            qs = qs.filter(created_at__date__lte=d_to)

        # reference_name/phone and the investor's name are encrypted, so neither
        # the referrer filter nor the search can be expressed in SQL. Both are
        # matched over the already-scoped rows and fed back as an id filter —
        # the same fallback the investor list uses, and the reason there is no
        # blind index to lean on here.
        ref = _digits(p.get('reference', ''))
        search = p.get('search', '').strip().lower()
        if ref or search:
            hits = []
            for pk, rn, rp, nm in qs.values_list('id', 'reference_name', 'reference_phone', 'investor__name'):
                if ref and _digits(rp)[-10:] != ref[-10:]:
                    continue
                if search and not (search in (rn or '').lower() or search in (rp or '').lower()
                                   or search in (nm or '').lower()):
                    continue
                hits.append(pk)
            qs = qs.filter(id__in=hits)

        if p.get('facets') in ('1', 'true'):
            # Only the values these rows hold, so no picker offers a choice that
            # returns nothing — same contract as the investor and payout lists.
            # Referrers are grouped by phone, which is stable; the name beside
            # it is free-typed and drifts.
            referrers, seen = [], set()
            for rn, rp in qs.values_list('reference_name', 'reference_phone'):
                # Last ten digits, the same comparison ?reference= makes.
                # Grouping on all the digits offered the same person twice when
                # one reward was typed 8866339595 and another +91 88663 39595.
                key = _digits(rp)[-10:] or (rn or '').strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                referrers.append({'phone': rp or '', 'name': rn or ''})
            return Response({
                'statuses': _distinct(qs, 'status'),
                'scheme_ids': _distinct(qs, 'investor__scheme_id'),
                'pcts': _distinct(qs, 'pct'),
                'referrers': sorted(referrers, key=lambda r: (r['name'] or '').lower()),
            })
        return Response(ReferralRewardSerializer(qs, many=True).data)


class ReferralRewardMarkPaidView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not club_can(request.user, 'club.payout.mark_paid'):
            return _no_permission()
        reward = _company_filtered(
            ReferralReward.objects.select_related('investor'), request, field='investor__company'
        ).filter(pk=pk).first()
        if not reward:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        # The Mark Paid dialog sends what went out (the reward unless changed), the
        # day it went out (today unless changed; never later) and remarks.
        amount = request.data.get('amount')
        if amount not in (None, ''):
            try:
                reward.paid_amount = Decimal(str(amount))
            except (InvalidOperation, ValueError, TypeError):
                return Response({'detail': 'Invalid amount.'}, status=status.HTTP_400_BAD_REQUEST)
            if reward.paid_amount < 0:
                return Response({'detail': 'The amount cannot be negative.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            reward.paid_amount = reward.amount
        paid_on = date.today()
        if request.data.get('paid_date'):
            paid_on = _parse_date(request.data.get('paid_date'))
            if not paid_on:
                return Response({'detail': 'Invalid paid date.'}, status=status.HTTP_400_BAD_REQUEST)
            if paid_on > date.today():
                return Response({'detail': 'The paid date cannot be in the future.'}, status=status.HTTP_400_BAD_REQUEST)
        if request.data.get('notes') is not None:
            reward.notes = request.data.get('notes') or ''
        reward.status = 'paid'
        reward.paid_date = paid_on
        reward.paid_by = request.user
        reward.save(update_fields=['status', 'paid_date', 'paid_amount', 'notes', 'paid_by', 'updated_at'])
        return Response(ReferralRewardSerializer(reward).data)


class ReferralRewardMarkUnpaidView(APIView):
    """Undo a referral reward marked paid by mistake — managers who may mark paid."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not (club_can(request.user, 'club.payout.mark_paid') and is_club1000_manager(request.user)):
            return _no_permission()
        reward = _company_filtered(
            ReferralReward.objects.select_related('investor'), request, field='investor__company'
        ).filter(pk=pk).first()
        if not reward:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if reward.status != 'paid':
            return Response({'detail': 'This reward is not marked paid.'}, status=status.HTTP_400_BAD_REQUEST)
        _undo_paid(reward)
        return Response(ReferralRewardSerializer(reward).data)


class PayoutPaymentView(_PaymentView):
    model = Payout
    serializer = PayoutSerializer
    noun = 'payout'


class ReferralRewardPaymentView(_PaymentView):
    model = ReferralReward
    serializer = ReferralRewardSerializer
    noun = 'reward'


class LeadListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = scope_leads_to_role(
            _company_filtered(Lead.objects.select_related('assigned_to', 'scheme_interest', 'created_by'), request),
            request.user,
        )
        if ai_capture(request, qs=qs):          # Ask Nexora: the leads this person's screen lists
            return Response({})
        if request.query_params.get('facets') == '1':
            return Response({
                'statuses': _distinct(qs, 'status'), 'sources': _distinct(qs, 'source'),
                'scheme_ids': _distinct(qs, 'scheme_interest_id'), 'assignee_ids': _distinct(qs, 'assigned_to_id'),
            })
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        if request.query_params.get('assigned_to'):
            qs = qs.filter(assigned_to_id=request.query_params['assigned_to'])
        if request.query_params.get('source'):
            qs = qs.filter(source=request.query_params['source'])
        if request.query_params.get('scheme_interest'):
            qs = qs.filter(scheme_interest_id=request.query_params['scheme_interest'])
        if request.query_params.get('date_from'):
            qs = qs.filter(created_at__date__gte=request.query_params['date_from'])
        if request.query_params.get('date_to'):
            qs = qs.filter(created_at__date__lte=request.query_params['date_to'])
        search = request.query_params.get('search', '').strip()
        if search:
            # Same as the investor list above: encrypted columns can't be matched
            # in SQL, so exact phone goes through the blind index and everything
            # else is matched in Python over the scoped rows.
            digits = ''.join(c for c in search if c.isdigit())
            if len(digits) >= 10 and not any(c.isalpha() for c in search):
                qs = qs.filter(phone_key=phone_blind_index(digits))
            else:
                needle = search.lower()
                hits = [pk for pk, nm, ph, em in qs.values_list('id', 'name', 'phone', 'email')
                        if needle in (nm or '').lower() or needle in (ph or '').lower()
                        or needle in (em or '').lower()]
                qs = qs.filter(id__in=hits)
        return Response(LeadListSerializer(qs, many=True).data)

    def post(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        data = request.data
        # Only managers/admins may hand a lead to someone else at creation —
        # mirrors the Add Lead form, which hides this control for anyone else.
        if not is_club1000_manager(request.user) and 'assigned_to' in data:
            data = {**data}
            data.pop('assigned_to', None)
        ser = LeadCreateSerializer(data=data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        defaults = {'company': request.user.company, 'created_by': request.user}
        if not ser.validated_data.get('assigned_to'):
            defaults['assigned_to'] = request.user
        lead = ser.save(**defaults)
        LeadStatusHistory.objects.create(
            lead=lead, changed_by=request.user, field_changed='created',
            new_value=lead.get_source_display(), remarks='Lead created',
        )
        return Response(LeadListSerializer(lead).data, status=status.HTTP_201_CREATED)


class LeadDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        qs = scope_leads_to_role(
            _company_filtered(Lead.objects.select_related('assigned_to', 'scheme_interest', 'created_by'), request),
            request.user,
        )
        return qs.filter(pk=pk).first()

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        lead = self._get(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        data = LeadListSerializer(lead).data
        recent = list(lead.history.order_by('-created_at', '-id')[:30])
        recent.reverse()  # oldest → newest for a top-to-bottom timeline
        data['history'] = LeadStatusHistorySerializer(recent, many=True).data
        return Response(data)

    def patch(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        lead = self._get(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        old_status = lead.status
        old_assigned_id = lead.assigned_to_id
        old_assigned_name = lead.assigned_to.name if lead.assigned_to_id else ''
        data = request.data
        # Only managers/admins may reassign a lead — mirrors the detail sheet,
        # which shows assignment as read-only for anyone else.
        if not is_club1000_manager(request.user) and 'assigned_to' in data:
            data = {**data}
            data.pop('assigned_to', None)
        ser = LeadListSerializer(lead, data=data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        lead = ser.save()
        # A lead reaching a terminal status has nothing left to follow up on.
        if lead.status in ('not_interested', 'lost', 'converted', 'investor_deleted') and lead.next_follow_up_date:
            lead.next_follow_up_date = None
            lead.save(update_fields=['next_follow_up_date'])
        history_entries = []
        if old_status != lead.status:
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='status', old_value=old_status, new_value=lead.status,
            ))
        if old_assigned_id != lead.assigned_to_id:
            new_assigned_name = lead.assigned_to.name if lead.assigned_to_id else ''
            history_entries.append(LeadStatusHistory(
                lead=lead, changed_by=request.user,
                field_changed='assigned_to', old_value=old_assigned_name, new_value=new_assigned_name,
            ))
            # Reassigning the lead hands off its still-open follow-ups too — a
            # completed/missed one stays on whoever actually did it (historical
            # record), but a not-yet-actioned one is still-open work that now
            # belongs to the new assignee.
            if lead.assigned_to_id:
                lead.follow_ups.filter(status__in=['pending', 'rescheduled']).update(assigned_to_id=lead.assigned_to_id)
        if history_entries:
            LeadStatusHistory.objects.bulk_create(history_entries)
        return Response(LeadListSerializer(lead).data)

    def delete(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        if not is_club1000_manager(request.user):
            return _no_permission()
        lead = self._get(request, pk)
        if not lead:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        lead.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FollowUpListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(
            FollowUp.objects.select_related('lead', 'lead__scheme_interest', 'assigned_to'), request, field='lead__company'
        )
        if not is_club1000_manager(request.user):
            qs = qs.filter(assigned_to=request.user)
        if ai_capture(request, qs=qs):          # Ask Nexora: the follow-ups this person's screen lists
            return Response({})
        if request.query_params.get('status'):
            qs = qs.filter(status=request.query_params['status'])
        return Response(FollowUpSerializer(qs, many=True).data)

    def post(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        ser = FollowUpSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        lead = ser.validated_data['lead']
        if lead.company_id != request.user.company_id and not is_platform_admin(request.user):
            return Response({'detail': 'Invalid lead for your company.'}, status=status.HTTP_400_BAD_REQUEST)
        followup = ser.save(
            created_by=request.user,
            assigned_to=ser.validated_data.get('assigned_to') or request.user,
        )
        # Scheduling a follow-up sets the lead's next follow-up date.
        lead.next_follow_up_date = followup.scheduled_at
        lead.save(update_fields=['next_follow_up_date'])
        return Response(FollowUpSerializer(followup).data, status=status.HTTP_201_CREATED)


class FollowUpDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        qs = _company_filtered(
            FollowUp.objects.select_related('lead', 'lead__scheme_interest', 'assigned_to'), request, field='lead__company'
        )
        if not is_club1000_manager(request.user):
            qs = qs.filter(assigned_to=request.user)
        followup = qs.filter(pk=pk).first()
        if not followup:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        old_scheduled_at = followup.scheduled_at
        ser = FollowUpSerializer(followup, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        followup = ser.save()
        # Keep the lead's next follow-up date in sync with this, its active follow-up.
        lead = followup.lead
        if lead.next_follow_up_date == old_scheduled_at:
            if followup.status in ('completed', 'missed'):
                lead.next_follow_up_date = None
                lead.save(update_fields=['next_follow_up_date'])
            elif followup.scheduled_at != old_scheduled_at:
                lead.next_follow_up_date = followup.scheduled_at
                lead.save(update_fields=['next_follow_up_date'])
        return Response(FollowUpSerializer(followup).data)


def _next_investor_loi_no(company, scheme):
    """Next per-scheme LOI number, e.g. 'RISE/028' — mirrors sales' _next_eoi_no."""
    existing = Investor.objects.filter(company=company, scheme=scheme).exclude(loi_no='').count()
    return f'{scheme.name}/{str(existing + 1).zfill(3)}'


def _investor_loi_path(investor):
    """Object storage path for a generated Investment Proposal Form — mirrors
    sales._loi_path's GAS-style layout."""
    import re
    # Also strips &%#+;= — an "&"/"+" etc. in the path breaks Supabase's signed-URL
    # scheme (confirmed against real Sales bookings — see sales.views._loi_path).
    san = lambda s: (re.sub(r'[\\/:*?"<>|&%#+;=]+', '', str(s or '')).strip() or 'NA')
    scheme_name = san(investor.scheme.name)
    client = san(investor.name)
    loi_no = san(investor.loi_no)
    return f'Club1000/{scheme_name}/{client} - {loi_no}/LOI.pdf'


def _investor_pending_loi_path(investor):
    """Storage path for a proposed revision's newly signed LOI — kept distinct
    from the live loi_document so a rejected revision never touches the
    original signed file."""
    import re
    # Also strips &%#+;= — an "&"/"+" etc. in the path breaks Supabase's signed-URL
    # scheme (confirmed against real Sales bookings — see sales.views._loi_path).
    san = lambda s: (re.sub(r'[\\/:*?"<>|&%#+;=]+', '', str(s or '')).strip() or 'NA')
    scheme_name = san(investor.scheme.name)
    client = san(investor.name)
    loi_no = san(investor.loi_no)
    return f'Club1000/{scheme_name}/{client} - {loi_no}/LOI_R{investor.revision_no}.pdf'


def _investor_visibility_qs(request):
    """Investor rows this user may view/act on — mirrors
    InvestorListCreateView.get's scoping exactly (own + scheme-approver)."""
    qs = _company_filtered(Investor.objects.select_related('scheme', 'added_by'), request)
    if not is_club1000_manager(request.user):
        approver_scheme_ids = _scheme_approver_ids(request.user, request.user.company)
        if approver_scheme_ids:
            qs = qs.filter(Q(added_by=request.user) | Q(scheme_id__in=approver_scheme_ids))
        else:
            qs = qs.filter(added_by=request.user)
    return qs


class InvestorNextLoiNoView(APIView):
    """Preview the next per-scheme LOI number before the investor even exists,
    so the pre-submit downloaded PDF shows the real value — mirrors
    sales.views.BookingNextEOIView. The actual assignment at submit time
    (InvestorListCreateView.post) re-runs the same counting helper; nothing
    is reserved here."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        scheme_id = request.query_params.get('scheme_id')
        if not scheme_id:
            return Response({'detail': 'scheme_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        scheme = _company_filtered(Scheme.objects.all(), request).filter(pk=scheme_id).first()
        if not scheme:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'loi_no': _next_investor_loi_no(request.user.company, scheme)})


def _notify_investor_approvers(company, investor, submitter):
    """Notify whoever should review a freshly-submitted investor — mirrors
    sales.views._notify_booking_approvers exactly, one level down (Scheme
    instead of Project)."""
    try:
        from notifications import notify, reporting_chain
        # 1) Per-scheme configured approvers (most precise).
        ids = (investor.scheme.investor_approvers if investor.scheme_id else None) or []
        recipients = list(User.objects.filter(id__in=ids, company=company, is_active=True)) if ids else []
        # 2) Fallback: the submitter's own reporting-manager chain.
        if not recipients:
            recipients = reporting_chain(submitter)
        # 3) Last resort: every manager/admin in the company (so it's never silent).
        if not recipients:
            recipients = list(User.objects.filter(company=company, is_active=True).filter(Q(role__in=MANAGER_ROLES) | Q(is_staff=True)))
        sub_id = getattr(submitter, 'id', None)
        recipients = [u for u in recipients if u and u.id != sub_id]
        if not recipients:
            return
        title = 'Investor approval needed'
        msg = '%s · %s · ₹%s — by %s' % (
            investor.name, investor.scheme.name if investor.scheme_id else '',
            int(investor.amount_invested or 0), getattr(submitter, 'name', ''),
        )
        seen = set()
        for u in recipients:
            if u.id not in seen:
                seen.add(u.id)
                notify(u, 'investor_approval', title, msg, {'investor_id': investor.id})
    except Exception:
        pass


class InvestorDraftView(APIView):
    """Save a half-filled investor, or update the one already saved.

    Mirrors sales' BookingDraftView, and for the same reason: the point is never
    to lose typed work, so none of the submit-time completeness checks apply.
    Only the scheme is required, because the maturity date is derived from its
    tenure and the form shows it back straight away.

    A draft belongs to whoever saved it and to nobody else — see the exclude() in
    InvestorListCreateView.get. It cannot be approved (InvestorActionView refuses
    one); submitting it POSTs to /investors/ with draft_id, which runs the real
    validation and turns this same row into a pending investor rather than
    leaving a duplicate behind.
    """
    permission_classes = [IsAuthenticated]

    DRAFTABLE = ('source', 'reference_name', 'reference_phone', 'name', 'phone',
                 'email', 'pan', 'notes', 'security', 'interest_payout')

    def post(self, request):
        if not has_club1000_access(request.user):
            return _no_access()
        data = request.data

        draft = None
        if data.get('id'):
            # A platform admin can edit any draft in the company, matching what
            # they can see — otherwise pressing Edit on someone else's draft would
            # quietly fork a second one of their own instead of saving theirs.
            owned = Q(added_by=request.user)
            if is_platform_admin(request.user):
                owned = Q()
            draft = (_company_filtered(Investor.objects.all(), request)
                     .filter(Q(pk=data['id'], approval_status='draft') & owned).first())

        scheme_id = data.get('scheme') or (draft.scheme_id if draft else None)
        scheme = Scheme.objects.filter(pk=scheme_id).first() if scheme_id else None
        if not scheme:
            return Response({'detail': 'Pick a scheme before saving a draft.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if scheme.company_id and scheme.company_id != request.user.company_id and not is_platform_admin(request.user):
            return Response({'detail': 'Invalid scheme for your company.'},
                            status=status.HTTP_400_BAD_REQUEST)

        investment_date = _parse_date(data.get('investment_date')) or (
            draft.investment_date if draft else date.today())
        fields = {
            'scheme': scheme,
            'investment_date': investment_date,
            'maturity_date': investment_date + relativedelta(months=scheme.tenure_months),
            # amount_invested is NOT NULL, and a draft is allowed to have no
            # figure yet — zero stands for "not said", and the submit path makes
            # it meet the scheme's minimum before anything is committed.
            'amount_invested': _draft_decimal(data.get('amount_invested'),
                                              draft.amount_invested if draft else 0),
            'total_return_pct': _draft_decimal(data.get('total_return_pct'),
                                               draft.total_return_pct if draft else 0),
        }
        for f in self.DRAFTABLE:
            if f in data:
                fields[f] = data.get(f) or ''
            elif draft is not None:
                fields[f] = getattr(draft, f)
        fields['interest_payout'] = fields.get('interest_payout') or (
            scheme.interest_payout_options[0] if scheme.interest_payout_options else 'maturity')
        fields['source'] = fields.get('source') or 'referral'

        if draft:
            for k, v in fields.items():
                setattr(draft, k, v)
            draft.save()
        else:
            draft = Investor.objects.create(
                company=request.user.company, added_by=request.user,
                approval_status='draft', **fields)
        return Response(InvestorListSerializer(draft).data,
                        status=status.HTTP_200_OK if data.get('id') else status.HTTP_201_CREATED)


class InvestorActionView(APIView):
    """Approve / reject a pending investor (approver = that scheme's configured
    investor_approvers, or a real admin — see can_approve_investor)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        investor = _company_filtered(Investor.objects.select_related('scheme', 'lead'), request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        # Enforced here and not merely by hiding the buttons in the list, or any
        # Club 1000 manager could approve another scheme's investor straight
        # through the API — the scheme's own investor_approvers picker (see
        # Investor Approvers — by scheme) would otherwise be purely cosmetic.
        if not (can_approve_investor(request.user, investor.scheme_id, request.user.company)
                or is_level2_approver(request.user, investor.scheme)):
            return Response(
                {'detail': 'You are not a configured approver for this scheme.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        action = request.data.get('action')
        if action not in ('approve', 'reject'):
            return Response({'detail': "action must be 'approve' or 'reject'."}, status=status.HTTP_400_BAD_REQUEST)
        if investor.approval_status == 'draft':
            # A draft skipped the completeness checks, so it may have no amount,
            # no KYC and no signed LOI — and approval is what generates the payout
            # schedule from those figures. It has to be submitted first.
            return Response(
                {'detail': 'This is still a draft. It has to be completed and submitted before it can be approved.'},
                status=status.HTTP_400_BAD_REQUEST)
        if investor.approval_status != 'pending':
            return Response({'detail': f'Investor is already {investor.approval_status}.'}, status=status.HTTP_400_BAD_REQUEST)

        from notifications import notify

        # A "revision" approval/rejection is one with proposed changes staged in
        # pending_revision (via InvestorReviseView or InvestorRenewView) —
        # distinct from an investor's maiden (first-ever) approval. Its
        # pending_revision_type tells apart a plain terms edit ('revise') from
        # a maturity rollover ('renew') — they're applied differently below.
        is_revision = bool(investor.pending_revision)
        revision_type = investor.pending_revision_type if is_revision else None
        is_renewal = revision_type == 'renew'
        is_redemption = revision_type == 'redeem'

        if action == 'approve':
            # Two-level schemes: level 1 passes it on; only level 2 makes it final.
            final, gate = approval_gate(request, investor, 'Redemption' if is_redemption else (
                'Renewal' if is_renewal else ('Revision' if is_revision else 'New investment')))
            if not final:
                return gate
            investor.approval_step = None
            Investor.objects.filter(pk=investor.pk).update(approval_step=None)
            if is_redemption:
                _apply_redemption(investor, request.user)
            elif is_revision:
                pr = investor.pending_revision or {}
                old_principal = investor.amount_invested or Decimal('0')
                # The term as it stood — kept as a past term if this is a renewal.
                finished = {'start': investor.investment_date.isoformat(), 'maturity': investor.maturity_date.isoformat(),
                            'principal': str(old_principal), 'return_pct': str(investor.total_return_pct or 0),
                            'interest_payout': investor.interest_payout} if investor.investment_date and investor.maturity_date else None
                if 'amount_invested' in pr:
                    investor.amount_invested = Decimal(str(pr['amount_invested']))
                if 'total_return_pct' in pr:
                    investor.total_return_pct = Decimal(str(pr['total_return_pct']))
                for f in ('interest_payout', 'security', 'notes', 'name', 'phone',
                          'email', 'pan', 'source', 'reference_name', 'reference_phone'):
                    if f in pr:
                        setattr(investor, f, pr[f])
                # The date the revised terms were agreed. Kept on the record
                # rather than only printed on the LOI, so a later reader can
                # tell when the figures in front of them started applying.
                rev_on = _parse_date(pr.get('revision_date'))
                if rev_on:
                    investor.revision_date = rev_on
                update_fields = [
                    'amount_invested', 'total_return_pct', 'interest_payout', 'security', 'notes',
                    'name', 'phone', 'phone_key', 'revision_date',
                    'email', 'pan', 'source', 'reference_name', 'reference_phone',
                    'loi_document', 'pending_loi_document', 'pending_revision', 'pending_revision_type',
                    'approval_status', 'updated_at',
                ]
                if not is_renewal and ('scheme' in pr or 'investment_date' in pr):
                    # A revised scheme or investment date moves maturity with it; the
                    # still-pending payouts are rebuilt from these below.
                    if pr.get('scheme'):
                        investor.scheme = Scheme.objects.get(pk=int(pr['scheme']))
                    new_date = _parse_date(pr.get('investment_date'))
                    if new_date:
                        investor.investment_date = new_date
                    investor.maturity_date = investor.investment_date + relativedelta(months=investor.scheme.tenure_months)
                    update_fields += ['scheme', 'investment_date', 'maturity_date']
                if pr.get('document_name'):
                    if investor.document:
                        try:
                            investor.document.delete(save=False)
                        except Exception:
                            pass
                    investor.document.name = pr['document_name']
                    update_fields.append('document')
                if is_renewal:
                    # A renewal restarts the investment term from the renewal
                    # date — same row, fresh maturity_date, back to 'active'
                    # even if it had drifted to 'matured' in the meantime.
                    try:
                        renewal_date = date.fromisoformat(str(pr['investment_date'])) if 'investment_date' in pr else date.today()
                    except (ValueError, TypeError):
                        renewal_date = date.today()
                    if not investor.first_invested_on:
                        investor.first_invested_on = investor.investment_date
                    investor.investment_date = renewal_date
                    investor.maturity_date = renewal_date + relativedelta(months=investor.scheme.tenure_months)
                    investor.status = 'active'
                    investor.renewal_count = (investor.renewal_count or 0) + 1
                    update_fields += ['investment_date', 'maturity_date', 'status', 'renewal_count', 'first_invested_on']
                if investor.loi_document:
                    if is_renewal and finished:
                        # The ended term keeps its signed LOI — readable from its Ledger
                        # term, and back in place if this renewal is ever undone.
                        finished['loi_document'] = investor.loi_document.name
                    else:
                        investor.loi_document.delete(save=False)
                if investor.pending_loi_document:
                    investor.loi_document.name = investor.pending_loi_document.name
                    investor.pending_loi_document = None
                payout_schedule = pr.get('payout_schedule')
                investor.pending_revision = None
                investor.pending_revision_type = 'revise'
                investor.approval_status = 'approved'
                investor.approved_by, investor.approved_at = request.user, timezone.now()
                investor.save(update_fields=update_fields + ['approved_by', 'approved_at'])
                # Regenerate the payout schedule from the revised terms — only
                # still-pending entries; anything already paid is left untouched.
                if is_renewal:
                    # Renewal: the finished term is kept (its ledger and payouts stay
                    # readable as their own term). Its unpaid interest is still owed;
                    # its maturity payout — principal (+ interest for an at-maturity
                    # scheme) — becomes what really happens at a renewal: the interest
                    # paid out (less any rolled into a bigger renewal) and the principal
                    # paid back when renewing for less. The rest rolls over.
                    paid_mat = bool(finished) and Payout.objects.filter(
                        investor=investor, payout_type='maturity', status='paid',
                        due_date=date.fromisoformat(finished['maturity'])).exists()
                    Payout.objects.filter(investor=investor, status='pending', payout_type='maturity').delete()
                    if finished:
                        new_principal = investor.amount_invested or Decimal('0')
                        # The finished term's unpaid interest is either paid to him (it stays
                        # due on Payouts) or added to the renewal — settled into the new
                        # principal, never overdue. Unsaid: added when he renews for more.
                        reinvest = (pr.get('interest_choice') or ('reinvest' if new_principal > old_principal else 'pay')) == 'reinvest'
                        room = max(new_principal - old_principal, Decimal('0')) if reinvest else Decimal('0')
                        if reinvest and pr.get('interest_added') not in (None, ''):
                            room = min(room, Decimal(str(pr['interest_added'])))
                        rolled = close_term(investor, finished, new_principal, maturity_paid=paid_mat, roll_room=room)
                        room -= rolled
                        if room > 0:
                            renewed_on = investor.investment_date
                            for p in Payout.objects.filter(investor=investor, status='pending', payout_type='interest',
                                                           due_date__lte=date.fromisoformat(finished['maturity'])).order_by('due_date', 'id'):
                                if room <= 0:
                                    break
                                part = min(room, p.amount_due)
                                _pay_payout(p, part, renewed_on, f'Settled to renewal R{investor.renewal_count} · added to the principal', request.user)
                                room -= part
                                rolled += part
                        # Handed over at the renewal: what stayed due to him from the ended
                        # term (its interest, principal taken back) is paid now.
                        # A renewal settles the term it ends: what stayed due to him from it
                        # (interest he takes, principal taken back, interest left unpaid in
                        # earlier instalments) is handed over at the renewal — paid on the
                        # renewal date (or the date given). Only the running term can owe.
                        pay_now = pr.get('pay_now') or {}
                        paid_on = (date.fromisoformat(pay_now['paid_date']) if pay_now.get('paid_date')
                                   else min(investor.investment_date, date.today()))
                        note_txt = f'Settled to renewal R{investor.renewal_count}' + (f' · {pay_now["notes"]}' if pay_now.get('notes') else '')
                        settled_any = False
                        for p in Payout.objects.filter(investor=investor, status='pending',
                                                       due_date__lte=date.fromisoformat(finished['maturity'])).order_by('due_date', 'id'):
                            _pay_payout(p, p.amount_due, paid_on, note_txt, request.user)
                            settled_any = True
                        if settled_any:
                            finished['paid_at_renewal'] = paid_on.isoformat()
                        finished['rolled_interest'] = str(rolled.quantize(Decimal('0.01')))
                        finished['interest_choice'] = 'reinvest' if reinvest else 'pay'
                        investor.past_terms = (investor.past_terms or []) + [finished]
                        investor.save(update_fields=['past_terms'])
                else:
                    Payout.objects.filter(investor=investor, status='pending').delete()
                generate_payout_schedule(investor, custom_entries=payout_schedule)
                # An investment approved before leads were made on approval may
                # still have none — every approval makes sure it has one.
                _ensure_converted_lead(investor, request.user)
            else:
                investor.approval_status = 'approved'
                investor.status = 'active'
                investor.approved_by, investor.approved_at = request.user, timezone.now()
                investor.save(update_fields=['approval_status', 'status', 'approved_by', 'approved_at', 'updated_at'])
                # Now — and only now — run the three deferred side effects.
                # An investor added directly has no lead; one is found by phone
                # or made here, so every approved investment sits in the funnel
                # it actually came through.
                _ensure_converted_lead(investor, request.user)
                generate_payout_schedule(investor, custom_entries=investor.pending_payout_schedule)
                if investor.past_terms:
                    generate_past_term_payouts(investor)
                investor.pending_payout_schedule = None
                investor.save(update_fields=['pending_payout_schedule'])

            # Referral reward: 0.5% of the amount on a new investment (this
            # approval isn't a revision at all — maiden approval), 0.25% of
            # the renewed amount on a Renew approval — each is a fresh
            # commission-worthy event, so it creates its OWN reward row (a
            # single Investor can be renewed more than once over its life). A
            # plain Revise, by contrast, doesn't create anything new — it just
            # rescales the most recent still-pending reward at ITS own
            # already-locked-in rate, leaving a paid one alone.
            ref_name, ref_phone = investor.reference_name, investor.reference_phone
            if not is_revision or is_renewal:
                pct = REINVESTMENT_REFERRAL_REWARD_PCT if is_renewal else REFERRAL_REWARD_PCT
                if ref_phone:
                    new_amount = (investor.amount_invested * pct / Decimal('100')).quantize(Decimal('0.01'))
                    ReferralReward.objects.create(
                        investor=investor, reference_name=ref_name, reference_phone=ref_phone, amount=new_amount, pct=pct,
                    )
            elif ref_phone:
                existing_reward = investor.referral_rewards.filter(status='pending').order_by('-created_at').first()
                if existing_reward:
                    new_amount = (investor.amount_invested * existing_reward.pct / Decimal('100')).quantize(Decimal('0.01'))
                    existing_reward.amount = new_amount
                    # A revision may correct who referred the investor.
                    existing_reward.reference_name, existing_reward.reference_phone = ref_name, ref_phone
                    existing_reward.save(update_fields=['amount', 'reference_name', 'reference_phone'])
                elif not investor.referral_rewards.exists():
                    # A reference added by the revision, on an investor that never had
                    # one: the maiden reward it would have earned.
                    new_amount = (investor.amount_invested * REFERRAL_REWARD_PCT / Decimal('100')).quantize(Decimal('0.01'))
                    ReferralReward.objects.create(
                        investor=investor, reference_name=ref_name, reference_phone=ref_phone, amount=new_amount, pct=REFERRAL_REWARD_PCT,
                    )
            try:
                approved_label = 'Investor renewal approved' if is_renewal else ('Investor revision approved' if is_revision else 'Investor approved')
                notify(investor.added_by, 'investor_approved', approved_label,
                       f'{investor.name} · {investor.scheme.name if investor.scheme_id else ""} was approved.',
                       {'investor_id': investor.id})
            except Exception:
                pass
        else:
            # A rejection always says why — whoever submitted it has to know what to fix.
            reason = (request.data.get('reason') or '').strip()[:1000]
            if not reason:
                return Response({'detail': 'Give a reason for rejecting.'}, status=status.HTTP_400_BAD_REQUEST)
            investor.rejected_reason = reason
            investor.approval_step = None
            investor.save(update_fields=['rejected_reason', 'approval_step'])
            if is_revision:
                # Reject the PROPOSED revision/renewal only — the original
                # approved terms and signed LOI are completely untouched.
                if investor.pending_loi_document:
                    investor.pending_loi_document.delete(save=False)
                staged_doc = (investor.pending_revision or {}).get('document_name')
                if staged_doc:
                    try:
                        from django.core.files.storage import default_storage
                        default_storage.delete(staged_doc)
                    except Exception:
                        pass
                # Kept on record, and the revision number goes back — R1 rejected
                # leaves the investment as it was, not reading R1.
                proposed = investor.pending_revision or {}
                investor.rejections = (investor.rejections or []) + [{
                    'kind': 'renewal' if is_renewal else 'revision', 'no': investor.revision_no,
                    'reason': reason, 'by': request.user.name or '', 'at': timezone.now().isoformat(),
                    'amount': str(proposed.get('amount_invested') or ''),
                }]
                investor.revision_no = max(0, (investor.revision_no or 0) - 1)
                investor.pending_revision = None
                investor.pending_revision_type = 'revise'
                investor.approval_status = 'approved'
                investor.save(update_fields=['pending_revision', 'pending_revision_type', 'pending_loi_document', 'approval_status',
                                             'rejections', 'revision_no', 'updated_at'])
            else:
                investor.approval_status = 'rejected'
                if investor.loi_document:
                    investor.loi_document.delete(save=False)
                investor.save(update_fields=['approval_status', 'loi_document', 'updated_at'])
            try:
                rejected_label = 'Investor renewal rejected' if is_renewal else ('Investor revision rejected' if is_revision else 'Investor rejected')
                notify(investor.added_by, 'investor_rejected', rejected_label,
                       f'{investor.name} · {investor.scheme.name if investor.scheme_id else ""} was rejected: {reason}',
                       {'investor_id': investor.id})
            except Exception:
                pass
        return Response(InvestorListSerializer(investor).data)


# Everything the Add Investor form holds can be revised (owner, 16 Oct 2026): the
# Revise LOI dialog IS that form, prefilled. Changing the scheme or the investment
# date moves maturity, and on approval every still-pending payout is rebuilt from
# the new terms (paid ones are history and stay). revision_date is still accepted
# from older clients.
REVISION_FIELDS = (
    'scheme', 'investment_date', 'amount_invested', 'total_return_pct', 'interest_payout',
    'security', 'notes', 'name', 'phone', 'email', 'pan', 'source',
    'reference_name', 'reference_phone', 'revision_date',
)


def _revision_terms(request, investor):
    """The proposed terms from a Revise LOI request, checked: a scheme of this
    investor's company, a real date, and at least that scheme's minimum ticket.
    Returns (terms, error_response)."""
    terms = {f: request.data[f] for f in REVISION_FIELDS if f in request.data}
    scheme = investor.scheme
    if terms.get('scheme') not in (None, ''):
        try:
            scheme = Scheme.objects.get(pk=int(terms['scheme']))
        except (Scheme.DoesNotExist, TypeError, ValueError):
            return None, Response({'detail': 'Unknown scheme.'}, status=status.HTTP_400_BAD_REQUEST)
        if scheme.company_id and scheme.company_id != investor.company_id:
            return None, Response({'detail': 'Invalid scheme for this company.'}, status=status.HTTP_400_BAD_REQUEST)
        terms['scheme'] = scheme.id
    else:
        terms.pop('scheme', None)
    if terms.get('investment_date') not in (None, ''):
        if not _parse_date(terms['investment_date']):
            return None, Response({'detail': 'Invalid investment_date.'}, status=status.HTTP_400_BAD_REQUEST)
    else:
        terms.pop('investment_date', None)
    if terms.get('source') and terms['source'] != 'referral':
        terms['reference_name'], terms['reference_phone'] = '', ''
    try:
        amount = Decimal(str(terms.get('amount_invested', investor.amount_invested)))
    except Exception:
        return None, Response({'detail': 'Invalid amount_invested.'}, status=status.HTTP_400_BAD_REQUEST)
    if amount < scheme.min_ticket_size:
        return None, Response({'detail': f'Minimum ticket size for {scheme.name} is {scheme.min_ticket_size}.'},
                              status=status.HTTP_400_BAD_REQUEST)
    return terms, None


class InvestorReviseDraftView(APIView):
    """Save a half-finished "Revise LOI" without submitting it.

    A revision cannot go for approval until the revised LOI has been
    downloaded, signed and uploaded again, which takes days. Until now the form
    had to be held open or filled again from scratch, so the work was simply
    retyped — or lost.

    Deliberately NOT stored in pending_revision: that field means "a revision
    is waiting for an approver", and putting an unsubmitted draft there would
    put the investor in front of approvers with nothing signed to approve. The
    investor stays exactly as it is — still approved, still live, nothing about
    the current terms touched.

    POST saves (and replaces) the draft; DELETE discards it.
    """
    permission_classes = [IsAuthenticated]

    # The same fields the submit path accepts, minus the signed LOI — a draft
    # exists precisely because that file is not in hand yet.
    FIELDS = REVISION_FIELDS

    def _investor(self, request, pk):
        if not has_club1000_access(request.user):
            return None, _no_access()
        investor = _investor_visibility_qs(request).filter(pk=pk).first()
        if not investor:
            return None, Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if investor.approval_status != 'approved':
            return None, Response({'detail': 'Only an approved investor can be revised.'},
                                  status=status.HTTP_400_BAD_REQUEST)
        return investor, None

    def post(self, request, pk):
        investor, err = self._investor(request, pk)
        if err:
            return err
        draft = {f: request.data[f] for f in self.FIELDS if f in request.data}
        schedule = request.data.get('payout_schedule')
        if isinstance(schedule, list):
            draft['payout_schedule'] = schedule
        # Nothing filled in is a discard, not a draft of emptiness.
        investor.revision_draft = draft or None
        investor.save(update_fields=['revision_draft', 'updated_at'])
        return Response(InvestorListSerializer(investor).data)

    def delete(self, request, pk):
        investor, err = self._investor(request, pk)
        if err:
            return err
        investor.revision_draft = None
        investor.save(update_fields=['revision_draft', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class InvestorReviseView(APIView):
    """Propose revised terms + a newly signed LOI for an already-approved
    investor ("Revise LOI") — mirrors sales' Booking revision flow, adapted to
    Club 1000's single-record model: rather than creating a new row (which
    would double-count the payout schedule and referral reward), the SAME
    investor row re-enters the approval queue with its proposed changes held
    in pending_revision/pending_loi_document until InvestorActionView applies
    or discards them."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = _investor_visibility_qs(request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if investor.approval_status != 'approved':
            return Response({'detail': 'Only an approved investor can be revised.'}, status=status.HTTP_400_BAD_REQUEST)
        loi = request.data.get('loi_file')
        if not isinstance(loi, dict) or not loi.get('data'):
            return Response({'detail': 'loi_file is required.'}, status=status.HTTP_400_BAD_REQUEST)

        pending, err = _revision_terms(request, investor)
        if err:
            return err
        # A new KYC / ID scan waits with the rest of the revision: stored under its
        # own name now, swapped in on approval, deleted if the revision is rejected.
        doc = request.data.get('document_file')
        if isinstance(doc, dict) and doc.get('data'):
            import base64
            from django.core.files.base import ContentFile
            from django.core.files.storage import default_storage
            ext = (doc.get('name') or '').rsplit('.', 1)[-1][:10] or 'bin'
            try:
                pending['document_name'] = default_storage.save(
                    f'club1000/investor_{investor.id}_doc_r{investor.revision_no + 1}.{ext}',
                    ContentFile(base64.b64decode(doc['data'])))
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Investor revision document save failed for %s', investor.id)
                return Response({'detail': 'Could not save the scan document.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        payout_schedule = request.data.get('payout_schedule')
        if isinstance(payout_schedule, list):
            pending['payout_schedule'] = payout_schedule

        investor.pending_revision = pending
        investor.pending_revision_type = 'revise'
        investor.revision_no += 1
        investor.approval_status = 'pending'
        # The draft has become a real submission; leaving it behind would
        # prefill the next revision with these same figures.
        investor.revision_draft = None
        import base64
        from django.core.files.base import ContentFile
        try:
            investor.pending_loi_document.save(
                _investor_pending_loi_path(investor), ContentFile(base64.b64decode(loi['data'])), save=False,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Investor pending LOI save failed for %s', investor.id)
            return Response({'detail': 'Could not save the signed LOI.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        investor.save(update_fields=['pending_revision', 'pending_revision_type', 'revision_no', 'approval_status', 'pending_loi_document', 'revision_draft', 'updated_at'])
        _notify_investor_approvers(investor.company, investor, request.user)
        return Response(InvestorListSerializer(investor).data, status=status.HTTP_201_CREATED)


class InvestorRenewView(APIView):
    """Propose renewed terms + a newly signed LOI for a MATURED investor
    ("Renew" at maturity) — same staged pending_revision/pending_loi_document
    mechanic as InvestorReviseView (so a rejected renewal leaves the original
    matured terms and LOI untouched), but distinguished via
    pending_revision_type='renew' so InvestorActionView.approve knows to also
    reset investment_date/maturity_date and apply the renewal referral-reward
    rate (REINVESTMENT_REFERRAL_REWARD_PCT) instead of a plain rescale."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = _investor_visibility_qs(request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not investor.is_matured():
            return Response({'detail': 'Only a matured investor can be renewed.'}, status=status.HTTP_400_BAD_REQUEST)
        loi = request.data.get('loi_file')
        if not isinstance(loi, dict) or not loi.get('data'):
            return Response({'detail': 'loi_file is required.'}, status=status.HTTP_400_BAD_REQUEST)

        pending = {}
        for f in ('amount_invested', 'total_return_pct', 'interest_payout', 'security', 'notes'):
            if f in request.data:
                pending[f] = request.data[f]
        try:
            amount = Decimal(str(pending.get('amount_invested', investor.amount_invested)))
        except Exception:
            return Response({'detail': 'Invalid amount_invested.'}, status=status.HTTP_400_BAD_REQUEST)
        if amount < investor.scheme.min_ticket_size:
            return Response(
                {'detail': f'Minimum ticket size for {investor.scheme.name} is {investor.scheme.min_ticket_size}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        renewal_date = request.data.get('investment_date') or str(date.today())
        try:
            date.fromisoformat(str(renewal_date))
        except (ValueError, TypeError):
            return Response({'detail': 'Invalid investment_date.'}, status=status.HTTP_400_BAD_REQUEST)
        pending['investment_date'] = renewal_date
        # The finished term's unpaid interest: paid out to him, or added to the renewal.
        choice = str(request.data.get('interest_choice') or '').lower()
        if choice in ('pay', 'reinvest'):
            pending['interest_choice'] = choice
        # The money going to him at this renewal (the ended term's interest he takes,
        # principal taken back) was handed over now: paid on approval, on this date.
        pn = request.data.get('pay_now')
        if isinstance(pn, dict) and (pn.get('paid_date') or pn.get('notes')):
            paid_on = _parse_date(pn.get('paid_date')) if pn.get('paid_date') else None
            if pn.get('paid_date') and (not paid_on or paid_on > date.today()):
                return Response({'detail': 'The paid date must be today or earlier.'}, status=status.HTTP_400_BAD_REQUEST)
            pending['pay_now'] = {**({'paid_date': paid_on.isoformat()} if paid_on else {}), 'notes': str(pn.get('notes') or '')[:200]}
        # Exactly how much of that interest goes into the renewal, when the form knows
        # (he took part of it and added fresh money besides).
        if request.data.get('interest_added') not in (None, ''):
            try:
                pending['interest_added'] = str(max(Decimal(str(request.data['interest_added'])), Decimal('0')))
            except Exception:
                return Response({'detail': 'Invalid interest_added.'}, status=status.HTTP_400_BAD_REQUEST)
        payout_schedule = request.data.get('payout_schedule')
        if isinstance(payout_schedule, list):
            pending['payout_schedule'] = payout_schedule

        investor.pending_revision = pending
        investor.pending_revision_type = 'renew'
        investor.revision_no += 1
        investor.approval_status = 'pending'
        import base64
        from django.core.files.base import ContentFile
        try:
            investor.pending_loi_document.save(
                _investor_pending_loi_path(investor), ContentFile(base64.b64decode(loi['data'])), save=False,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Investor pending LOI save failed for %s', investor.id)
            return Response({'detail': 'Could not save the signed LOI.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        investor.save(update_fields=['pending_revision', 'pending_revision_type', 'revision_no', 'approval_status', 'pending_loi_document', 'updated_at'])
        _notify_investor_approvers(investor.company, investor, request.user)
        return Response(InvestorListSerializer(investor).data, status=status.HTTP_201_CREATED)


class InvestorUndoRenewalView(APIView):
    """POST — undo the latest renewal (managers): the investment goes back to the term
    before it. The renewed term's payouts and its renewal referral reward go; interest
    that was added to the renewal is due again; the ended term's maturity payout is put
    back; and its signed LOI is restored if it was kept. Money already paid in the
    renewed term (or its reward paid) stops it unless confirm_paid — the screen shows
    what will go and asks first."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        if not is_club1000_manager(request.user):
            return Response({'detail': 'Only a Club 1000 manager can undo a renewal.'}, status=status.HTTP_403_FORBIDDEN)
        investor = _company_filtered(Investor.objects.select_related('scheme'), request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if investor.approval_status != 'approved':
            return Response({'detail': 'Finish the pending approval first.'}, status=status.HTTP_400_BAD_REQUEST)
        past = list(investor.past_terms or [])
        if not investor.renewal_count or len(past) < investor.renewal_count:
            return Response({'detail': 'This renewal was made before Nexora kept the term it replaced, so it cannot be '
                                       'undone automatically.'}, status=status.HTTP_400_BAD_REQUEST)
        last = past[-1]
        terms = term_list(investor)
        cur_no = terms[-1]['no']
        ended_maturity = date.fromisoformat(last['maturity'])
        all_pays = list(investor.payouts.all())
        current = [p for p in all_pays if term_of(terms, p.due_date) == cur_no]
        made_at_renewal = [p for p in all_pays if (p.notes or '').startswith(f'Term ending {ended_maturity:%d-%m-%Y}')]
        settled = [p for p in all_pays if (p.notes or '').startswith((f'Added to renewal R{investor.renewal_count}',
                                                                      f'Settled at renewal R{investor.renewal_count}',
                                                                      f'Settled to renewal R{investor.renewal_count}'))]
        reward = investor.referral_rewards.filter(pct=REINVESTMENT_REFERRAL_REWARD_PCT).order_by('-created_at').first()
        paid_rows = [p for p in current + made_at_renewal if p.status == 'paid' and p not in settled]
        reward_paid = bool(reward and reward.status == 'paid')
        executing = getattr(request, '_c1_execute', False)        # an approver approved the request
        confirm = executing or str(request.data.get('confirm_paid') or '').lower() in ('1', 'true', 'yes')
        if (paid_rows or reward_paid) and not confirm:
            amt = sum(((p.paid_amount if p.paid_amount is not None else p.amount_due) or Decimal('0') for p in paid_rows), Decimal('0'))
            return Response({'detail': f'{len(paid_rows)} payment{"s" if len(paid_rows) != 1 else ""} (₹{amt:,.0f}) already made in the '
                                       f'renewed term{" and its referral reward is paid" if reward_paid else ""}.',
                             'code': 'has_paid', 'paid_payouts': len(paid_rows), 'paid_amount': str(amt),
                             'reward_paid': reward_paid}, status=status.HTTP_409_CONFLICT)
        if not executing:
            # Not done here: an undo goes to the scheme's approvers (Approvals → Pending).
            reason = (request.data.get('reason') or '').strip()[:1000]
            if not reason:
                return Response({'detail': 'Give a reason for undoing the renewal.'}, status=status.HTTP_400_BAD_REQUEST)
            _request_action(request, investor, 'undo_renewal', reason, bool(paid_rows or reward_paid))
            return Response({'requested': True, 'detail': 'Sent to the approvers — the renewal is undone once approved.'},
                            status=status.HTTP_202_ACCEPTED)
        renewed_label = f'{investor.investment_date:%d-%m-%Y} → {investor.maturity_date:%d-%m-%Y} · ₹{investor.amount_invested:,.0f}'
        with transaction.atomic():
            for p in settled:
                _undo_paid(p)                          # added to the renewal → due again
            Payout.objects.filter(pk__in=[p.pk for p in current + made_at_renewal]).delete()
            if reward:
                reward.delete()
            new_loi = investor.loi_document.name if investor.loi_document else ''
            investor.amount_invested = Decimal(str(last['principal']))
            investor.total_return_pct = Decimal(str(last.get('return_pct') or investor.total_return_pct or 0))
            investor.interest_payout = last.get('interest_payout') or investor.interest_payout
            investor.investment_date = date.fromisoformat(last['start'])
            investor.maturity_date = ended_maturity
            investor.renewal_count = max(0, investor.renewal_count - 1)
            investor.revision_no = max(0, (investor.revision_no or 0) - 1)
            investor.past_terms = past[:-1] or None
            investor.status = 'active'
            if last.get('loi_document'):
                investor.loi_document.name = last['loi_document']
            investor.save()
            # The ended term's own maturity payout, as it stood before the renewal.
            if not Payout.objects.filter(investor=investor, payout_type='maturity', due_date=ended_maturity).exists():
                at_maturity = investor.interest_payout not in ('quarterly', 'monthly')
                amount = (maturity_value(investor.amount_invested, investor.total_return_pct, investor.investment_date, ended_maturity)
                          if at_maturity else investor.amount_invested)
                Payout.objects.create(investor=investor, payout_type='maturity', due_date=ended_maturity,
                                      amount_due=amount.quantize(Decimal('0.01')))
        # The undone renewal's signed LOI is kept in storage (never deleted) and noted.
        if new_loi and new_loi != (investor.loi_document.name if investor.loi_document else ''):
            investor.rejections = (investor.rejections or []) + [{'kind': 'undone-renewal', 'no': investor.renewal_count + 1,
                                                                  'reason': 'renewal undone', 'loi_document': new_loi,
                                                                  'at': timezone.now().isoformat(), 'by': request.user.name or ''}]
            investor.save(update_fields=['rejections'])
        from activity.recorder import note
        note(request, f'Undid renewal — {investor.name}{f" · LOI {investor.loi_no}" if investor.loi_no else ""} · removed the '
                      f'renewed term {renewed_label}; back to {investor.investment_date:%d-%m-%Y} → {investor.maturity_date:%d-%m-%Y}',
             action='undo-renewal', target_type='investor', target_id=str(investor.pk), module='Club 1000')
        return Response(InvestorListSerializer(investor).data)


class InvestorPendingActionView(APIView):
    """POST {action: approve|reject, reason} — an approver decides a requested delete
    or undo-renewal (filed by Delete / Undo renewal). Approve carries it out; reject
    clears it and keeps the rejection on record."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not is_club1000_manager(request.user):
            return _no_permission()
        investor = _company_filtered(Investor.objects.select_related('scheme', 'lead'), request).filter(pk=pk).first()
        if not investor or not investor.pending_action:
            return Response({'detail': 'Nothing is waiting for approval on this investor.'}, status=status.HTTP_404_NOT_FOUND)
        if not (can_approve_investor(request.user, investor.scheme_id, request.user.company)
                or is_level2_approver(request.user, investor.scheme)):
            return Response({'detail': 'You are not a configured approver for this scheme.'}, status=status.HTTP_403_FORBIDDEN)
        pa = investor.pending_action
        kind = pa.get('type')
        action = request.data.get('action')
        from activity.recorder import note
        what = 'delete' if kind == 'delete' else 'undo renewal'
        if action == 'reject':
            reason = (request.data.get('reason') or '').strip()[:1000]
            if not reason:
                return Response({'detail': 'Give a reason for rejecting.'}, status=status.HTTP_400_BAD_REQUEST)
            investor.rejections = (investor.rejections or []) + [{
                'kind': 'delete' if kind == 'delete' else 'undo-renewal', 'no': investor.revision_no, 'reason': reason,
                'by': request.user.name or '', 'at': timezone.now().isoformat(),
                'asked_by': pa.get('by_name', ''), 'asked_reason': pa.get('reason', '')}]
            investor.pending_action = None
            investor.approval_step = None
            investor.save(update_fields=['rejections', 'pending_action', 'approval_step'])
            note(request, f'Rejected the {what} of {investor.name}: {reason}', action='reject', target_type='investor',
                 target_id=str(investor.pk), module='Club 1000')
            try:
                from notifications import notify
                asker = User.objects.filter(pk=pa.get('by')).first()
                if asker:
                    notify(asker, 'investor_rejected', f'{what.capitalize()} rejected', f'{investor.name}: {reason}', {'investor_id': investor.id})
            except Exception:
                pass
            return Response(InvestorListSerializer(investor).data)
        if action != 'approve':
            return Response({'detail': "action must be 'approve' or 'reject'."}, status=status.HTTP_400_BAD_REQUEST)
        final, gate = approval_gate(request, investor, 'Delete' if kind == 'delete' else 'Undo renewal')
        if not final:
            return gate
        investor.pending_action = None
        investor.approval_step = None
        investor.save(update_fields=['pending_action', 'approval_step'])
        if kind == 'delete':
            why, _ = _delete_investor(request, investor, include_paid=True)
            if why:
                return Response({'detail': why}, status=status.HTTP_409_CONFLICT)
            return Response({'deleted': True})
        request._c1_execute = True
        return InvestorUndoRenewalView().post(request, pk)


class InvestorLoiNoView(APIView):
    """Reserves (assigning + persisting if not already set) and returns this
    investor's LOI number — called before PDF generation so the number is
    stable and can be baked into the document itself, then just reused (not
    reassigned) when the generated PDF is uploaded via InvestorUploadLoiView."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = _company_filtered(Investor.objects.select_related('scheme'), request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not investor.loi_no:
            investor.loi_no = _next_investor_loi_no(investor.company, investor.scheme)
            investor.save(update_fields=['loi_no'])
        return Response({'loi_no': investor.loi_no})


class InvestorUploadLoiView(APIView):
    """Stores the client-generated Investment Proposal Form PDF for an investor
    and assigns its LOI number if not already set — mirrors how Sales' signed
    LOI is saved in BookingListCreateView.post."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not has_club1000_access(request.user):
            return _no_access()
        investor = _company_filtered(Investor.objects.select_related('scheme'), request).filter(pk=pk).first()
        if not investor:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        loi = request.data.get('loi_file')
        if not isinstance(loi, dict) or not loi.get('data'):
            return Response({'detail': 'loi_file is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not investor.loi_no:
            investor.loi_no = _next_investor_loi_no(investor.company, investor.scheme)
            investor.save(update_fields=['loi_no'])
        import base64
        from django.core.files.base import ContentFile
        try:
            investor.loi_document.save(_investor_loi_path(investor), ContentFile(base64.b64decode(loi['data'])), save=True)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Investor LOI save failed for %s', investor.id)
            return Response({'detail': 'Could not save LOI document.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(InvestorListSerializer(investor).data)


class InvestorLoiUrlView(APIView):
    """Short-lived signed URL for an investor's generated LOI PDF — mirrors
    sales.views.BookingLOIUrlView exactly. ?pending=1 serves the not-yet-approved
    revision's newly signed LOI instead of the live one (see InvestorReviseView)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        investor = _company_filtered(Investor.objects.all(), request).filter(pk=pk).first()
        wants_pending = request.query_params.get('pending') == '1'
        doc = investor.pending_loi_document if (investor and wants_pending) else (investor.loi_document if investor else None)
        # ?term=N — a renewed investment's earlier term (0 = the original) keeps its own LOI.
        term = request.query_params.get('term')
        if investor and term not in (None, ''):
            past = investor.past_terms or []
            try:
                t = past[int(term)]
            except (ValueError, IndexError):
                t = None
            if not t or not t.get('loi_document'):
                return Response({'detail': "This term's signed LOI was not kept — it was renewed before Nexora kept old LOIs."},
                                status=status.HTTP_404_NOT_FOUND)
            from types import SimpleNamespace
            doc = SimpleNamespace(name=t['loi_document'], url='')
        # ?payout=N — a premature redemption's own signed LOI, kept on the
        # payout because the investor's copy is replaced by whatever is signed
        # next.
        payout_id = request.query_params.get('payout')
        if investor and payout_id not in (None, ''):
            po = investor.payouts.filter(pk=payout_id).first()
            if not po or not po.loi_document:
                return Response({'detail': 'This payout has no signed LOI of its own.'},
                                status=status.HTTP_404_NOT_FOUND)
            doc = po.loi_document
        if not investor or not doc:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        is_scheme_approver = investor.scheme_id in _scheme_approver_ids(request.user, request.user.company)
        if not (is_club1000_manager(request.user) or investor.added_by_id == request.user.id or is_scheme_approver):
            return Response({'detail': 'Permission denied.'}, status=status.HTTP_403_FORBIDDEN)
        from sales.supabase_storage import create_signed_url
        url = create_signed_url(doc.name, expires_in=120)
        if not url:
            try:
                url = request.build_absolute_uri(doc.url)
            except Exception:
                url = None
        if not url:
            return Response({'detail': 'LOI unavailable.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'url': url})
