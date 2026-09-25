"""Collections: who owes what now, what falls due next, and the follow-ups that
chase it.

The dues come straight from the AR engine (the same numbers as the register and
ledger); a follow-up is the only thing stored — who calls which customer, when,
what was said and what they promised.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import is_platform_admin, scope_to_company
from .engine import rupees
from .models import ARAccount, ARFollowUp
from .permissions import ar_can, has_ar_access
from .services import parse_date, _d
from .views import _accounts_qs, _as_of, _computed, _deny, _log, _summary, _sync

ZERO = Decimal('0')
CHANNELS = dict(ARFollowUp.CHANNELS)


def _parse_when(raw):
    """A follow-up time: an ISO datetime, or a bare date meaning 10:00 that day."""
    if not raw:
        return None
    raw = str(raw).strip()
    dt = parse_datetime(raw)
    if dt is None:
        d = parse_date(raw)
        if d is None:
            return None
        dt = datetime.combine(d, time(10, 0))
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _money(raw):
    if raw in (None, ''):
        return None
    try:
        v = Decimal(str(raw).replace(',', ''))
    except (InvalidOperation, ValueError):
        raise ValueError('Promised amount must be a number.')
    if v < 0:
        raise ValueError('Promised amount cannot be negative.')
    return v


def ar_users(company):
    """Active users of `company` who can work Accounts Receivable."""
    return [u for u in User.objects.filter(company=company, is_active=True).order_by('name')
            if has_ar_access(u)]


def _account_brief(acct):
    b = acct.booking
    return {
        'id': acct.id,
        'client_name': b.client_name or '',
        'phone': b.phone or '',
        'project': b.project.name if b.project_id else '',
        'plots': b.plot_numbers or (b.plot.number if b.plot_id else ''),
    }


def serialize_followup(f, now=None, with_account=False):
    now = now or timezone.now()
    out = {
        'id': f.id,
        'account_id': f.account_id,
        'scheduled_at': f.scheduled_at.isoformat(),
        'channel': f.channel,
        'channel_label': CHANNELS.get(f.channel, f.channel),
        'status': f.status,
        'note': f.note or '',
        'outcome': f.outcome or '',
        'promised_amount': rupees(_d(f.promised_amount)) if f.promised_amount is not None else None,
        'promised_on': f.promised_on.isoformat() if f.promised_on else None,
        'assigned_to': {'id': f.assigned_to_id, 'name': f.assigned_to.name} if f.assigned_to_id else None,
        'created_by': f.created_by.name if f.created_by_id else '',
        'created_at': f.created_at.isoformat() if f.created_at else None,
        'done_by': f.done_by.name if f.done_by_id else '',
        'done_at': f.done_at.isoformat() if f.done_at else None,
        'is_overdue': f.status == 'pending' and f.scheduled_at < now,
    }
    if with_account:
        out['account'] = _account_brief(f.account)
    return out


def _followup_qs(request):
    qs = ARFollowUp.objects.select_related(
        'account', 'account__booking', 'account__booking__project', 'account__booking__plot',
        'assigned_to', 'created_by', 'done_by')
    qs = scope_to_company(qs, request.user, 'account__company')
    cid = request.query_params.get('company_id')
    if cid and is_platform_admin(request.user):
        qs = qs.filter(account__company_id=cid)
    return qs


def _notify_assigned(f, by):
    if not f.assigned_to_id or f.assigned_to_id == getattr(by, 'id', None):
        return
    try:
        from notifications import notify
        b = f.account.booking
        unit = b.plot_numbers or (b.plot.number if b.plot_id else '')
        when = timezone.localtime(f.scheduled_at).strftime('%d %b, %I:%M %p')
        notify(f.assigned_to, 'ar_followup_assigned', 'Collection follow-up assigned',
               '%s · %s Plot %s — %s on %s (from %s)' % (
                   b.client_name or 'Customer', b.project.name if b.project_id else '', unit,
                   CHANNELS.get(f.channel, f.channel), when, getattr(by, 'name', '') or 'AR'),
               {'account_id': f.account_id, 'followup_id': f.id})
    except Exception:
        pass


def _resolve_assignee(request, acct, raw):
    """The user a follow-up goes to: someone with AR access in the account's company."""
    if raw in (None, ''):
        return request.user
    u = User.objects.filter(pk=raw, company_id=acct.company_id, is_active=True).first()
    if not u or not has_ar_access(u):
        raise ValueError('Assign the follow-up to someone with Accounts Receivable access.')
    return u


def collection_row(acct, plan, r, receipts, as_of, window_end, fu):
    """One account's collections picture as of `as_of`."""
    open_lines = sorted((s for s in r.lines if s.remaining > 0 and s.line.due),
                        key=lambda s: s.line.due)
    overdue_lines = [s for s in open_lines if s.line.due <= as_of]
    later = [s for s in open_lines if s.line.due > as_of]
    in_window = [s for s in later if s.line.due <= window_end]
    oldest = overdue_lines[0].line.due if overdue_lines else None
    nxt = later[0] if later else None
    if window_end == as_of:
        # "Today": what falls due today (those lines also count in overdue from today).
        in_window = [s for s in open_lines if s.line.due == as_of]
        nxt = in_window[0] if in_window else nxt
    last = max(receipts, key=lambda x: (x.paid_on, x.id)) if receipts else None
    sm = _summary(acct, plan, r, ZERO)
    return {
        **_account_brief(acct),
        'project_id': sm['project_id'],
        'overdue': sm['overdue'],
        'os_with_interest': sm['os_with_interest'],
        'net_interest': sm['net_interest'],
        'outstanding': sm['outstanding'],
        'overdue_since': oldest.isoformat() if oldest else None,
        'days_overdue': (as_of - oldest).days if oldest else 0,
        'overdue_installments': len(overdue_lines),
        'next_due': {'date': nxt.line.due.isoformat(), 'amount': rupees(nxt.remaining),
                     'label': nxt.line.label} if nxt else None,
        'upcoming_amount': rupees(sum((s.remaining for s in in_window), ZERO)),
        'upcoming_installments': len(in_window),
        'last_paid_on': last.paid_on.isoformat() if last else None,
        'last_paid_amount': rupees(_d(last.amount)) if last else None,
        'followup': fu.get('next'),
        'followups_done': fu.get('done', 0),
        'last_outcome': fu.get('last_outcome'),
    }


def _followup_index(account_ids, now):
    """Per account: its next pending follow-up, how many are done, the last outcome."""
    idx = {}
    qs = (ARFollowUp.objects.filter(account_id__in=account_ids).exclude(status='cancelled')
          .select_related('assigned_to').order_by('scheduled_at', 'id'))
    for f in qs:
        e = idx.setdefault(f.account_id, {'done': 0})
        if f.status == 'pending' and 'next' not in e:
            e['next'] = {'id': f.id, 'scheduled_at': f.scheduled_at.isoformat(),
                         'channel_label': CHANNELS.get(f.channel, f.channel),
                         'assigned_to': f.assigned_to.name if f.assigned_to_id else '',
                         'is_overdue': f.scheduled_at < now}
        elif f.status == 'done':
            e['done'] += 1
            if not e.get('last_at') or (f.done_at and f.done_at >= e['last_at']):
                e['last_at'] = f.done_at or f.scheduled_at
                e['last_outcome'] = {'text': f.outcome or '', 'at': (f.done_at or f.scheduled_at).isoformat(),
                                     'promised_amount': rupees(_d(f.promised_amount)) if f.promised_amount is not None else None,
                                     'promised_on': f.promised_on.isoformat() if f.promised_on else None}
    return idx


def collections_snapshot(accounts_qs, as_of, days=30):
    """Every active account's collections row, plus portfolio counts."""
    window_end = as_of + timedelta(days=days)
    now = timezone.now()
    computed = _computed(accounts_qs.filter(status='active'), as_of)
    fidx = _followup_index([a.id for a, *_ in computed], now)
    rows = [collection_row(a, p, r, rc, as_of, window_end, fidx.get(a.id, {}))
            for a, p, r, m, rc in computed]
    return rows, window_end


class ARCollectionsView(APIView):
    """Overdue and upcoming payments, each account with its next follow-up."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_ar_access(request.user):
            return _deny()
        _sync(request)
        as_of = _as_of(request)
        try:
            raw_days = request.query_params.get('days')
            # 0 = due today; otherwise the next N days.
            days = max(0, min(365, int(raw_days if raw_days not in (None, '') else 30)))
        except ValueError:
            days = 30
        qs = _accounts_qs(request)
        projects = {}
        for pid, name in qs.filter(status='active').values_list('booking__project_id', 'booking__project__name').distinct():
            if pid:
                projects[pid] = name or ''
        pid = request.query_params.get('project')
        if pid:
            qs = qs.filter(booking__project_id=pid)
        rows, window_end = collections_snapshot(qs, as_of, days)
        overdue = [x for x in rows if x['overdue'] > 0]
        upcoming = [x for x in rows if x['upcoming_amount'] > 0]
        now = timezone.now()
        fu = _followup_qs(request).filter(status='pending', account_id__in=[x['id'] for x in rows])
        end_of_today = timezone.make_aware(datetime.combine(timezone.localdate(), time.max))
        view = request.query_params.get('view') or 'overdue'
        if view == 'upcoming':
            shown = sorted(upcoming, key=lambda x: (x['next_due']['date'] if x['next_due'] else '9999', -x['upcoming_amount']))
        elif view == 'all':
            shown = sorted(rows, key=lambda x: (-x['overdue'], x['project'], str(x['plots'])))
        else:
            shown = sorted(overdue, key=lambda x: (-x['days_overdue'], -x['overdue']))
        return Response({
            'as_of': as_of.isoformat(),
            'window_end': window_end.isoformat(),
            'days': days,
            'projects': [{'id': k, 'name': v} for k, v in sorted(projects.items(), key=lambda kv: kv[1])],
            'counts': {
                'overdue_accounts': len(overdue),
                'overdue_amount': sum(x['overdue'] for x in overdue),
                'upcoming_accounts': len(upcoming),
                'upcoming_amount': sum(x['upcoming_amount'] for x in upcoming),
                'followups_overdue': fu.filter(scheduled_at__lt=now).count(),
                'followups_today': fu.filter(scheduled_at__gte=now, scheduled_at__lte=end_of_today).count(),
                'no_followup': sum(1 for x in overdue if not x['followup']),
            },
            'results': shown,
        })


class ARAccountFollowUpsView(APIView):
    """An account's follow-up history, newest first, and adding a new one."""
    permission_classes = [IsAuthenticated]

    def _acct(self, request, pk):
        return (scope_to_company(ARAccount.objects.select_related('booking', 'booking__project', 'booking__plot'),
                                 request.user).filter(pk=pk).first())

    def get(self, request, pk):
        if not has_ar_access(request.user):
            return _deny()
        acct = self._acct(request, pk)
        if not acct:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        now = timezone.now()
        items = _followup_qs(request).filter(account=acct).order_by('-scheduled_at', '-id')
        return Response({'account': _account_brief(acct),
                         'results': [serialize_followup(f, now) for f in items]})

    def post(self, request, pk):
        if not ar_can(request.user, 'ar.followup.manage'):
            return _deny()
        acct = self._acct(request, pk)
        if not acct:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        when = _parse_when(request.data.get('scheduled_at'))
        if not when:
            return Response({'detail': 'Pick when to follow up.'}, status=status.HTTP_400_BAD_REQUEST)
        channel = request.data.get('channel') or 'call'
        if channel not in CHANNELS:
            return Response({'detail': 'Unknown channel.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            assignee = _resolve_assignee(request, acct, request.data.get('assigned_to'))
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        f = ARFollowUp.objects.create(
            account=acct, scheduled_at=when, channel=channel,
            note=(request.data.get('note') or '').strip()[:2000],
            assigned_to=assignee, created_by=request.user)
        _notify_assigned(f, request.user)
        _log(request, acct, 'Scheduled %s follow-up for %s (assigned to %s)' % (
            CHANNELS[channel].lower(), timezone.localtime(when).strftime('%d/%m/%Y %I:%M %p'), assignee.name),
            'created', 'ar_account', acct.id)
        f = _followup_qs(request).get(pk=f.pk)
        return Response(serialize_followup(f), status=status.HTTP_201_CREATED)


class ARFollowUpView(APIView):
    """Close a follow-up (what happened, what was promised — optionally booking the
    next one), reschedule it, reassign it, or cancel it."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, fid):
        if not ar_can(request.user, 'ar.followup.manage'):
            return _deny()
        f = _followup_qs(request).filter(pk=fid).first()
        if not f:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        d = request.data
        fields = []
        new_status = d.get('status')
        try:
            if 'scheduled_at' in d:
                when = _parse_when(d.get('scheduled_at'))
                if not when:
                    raise ValueError('Invalid follow-up time.')
                f.scheduled_at = when
                # A new time is a new reminder.
                f.reminder_sent_at = None
                f.escalated_at = None
                fields += ['scheduled_at', 'reminder_sent_at', 'escalated_at']
            if 'channel' in d:
                if d['channel'] not in CHANNELS:
                    raise ValueError('Unknown channel.')
                f.channel = d['channel']
                fields.append('channel')
            if 'note' in d:
                f.note = (d.get('note') or '').strip()[:2000]
                fields.append('note')
            reassigned = False
            if 'assigned_to' in d:
                u = _resolve_assignee(request, f.account, d.get('assigned_to'))
                reassigned = u.id != f.assigned_to_id
                f.assigned_to = u
                fields.append('assigned_to')
            if new_status == 'done':
                f.status = 'done'
                f.outcome = (d.get('outcome') or '').strip()[:2000]
                if not f.outcome:
                    raise ValueError('Say what happened on this follow-up.')
                f.promised_amount = _money(d.get('promised_amount'))
                raw_on = d.get('promised_on')
                f.promised_on = parse_date(raw_on) if raw_on else None
                if raw_on and f.promised_on is None:
                    raise ValueError('Invalid promised date.')
                f.done_by = request.user
                f.done_at = timezone.now()
                fields += ['status', 'outcome', 'promised_amount', 'promised_on', 'done_by', 'done_at']
            elif new_status == 'cancelled':
                f.status = 'cancelled'
                fields.append('status')
            elif new_status not in (None, ''):
                raise ValueError('status must be done or cancelled.')
            nxt_when = _parse_when(d.get('next_at')) if new_status == 'done' and d.get('next_at') else None
            if new_status == 'done' and d.get('next_at') and not nxt_when:
                raise ValueError('Invalid next follow-up time.')
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if fields:
            f.save(update_fields=list(dict.fromkeys(fields)))
            if new_status == 'done':
                promise = ''
                if f.promised_amount is not None:
                    promise = '; promised ₹{:,}{}'.format(rupees(_d(f.promised_amount)),
                                                          ' by ' + f.promised_on.strftime('%d/%m/%Y') if f.promised_on else '')
                _log(request, f.account, 'Follow-up done: %s%s' % (f.outcome, promise), 'done', 'ar_account', f.account_id)
            elif new_status == 'cancelled':
                _log(request, f.account, 'Cancelled a follow-up', 'cancelled', 'ar_account', f.account_id)
            else:
                _log(request, f.account, 'Changed a follow-up (%s)' % ', '.join(dict.fromkeys(
                    x.replace('_', ' ') for x in fields if x not in ('reminder_sent_at', 'escalated_at'))),
                    'updated', 'ar_account', f.account_id)
        if reassigned:
            _notify_assigned(f, request.user)
        nxt = None
        if nxt_when:
            nxt = ARFollowUp.objects.create(
                account=f.account, scheduled_at=nxt_when, channel=d.get('next_channel') if d.get('next_channel') in CHANNELS else f.channel,
                note=(d.get('next_note') or '').strip()[:2000], assigned_to=f.assigned_to or request.user,
                created_by=request.user)
            _notify_assigned(nxt, request.user)
        f = _followup_qs(request).get(pk=f.pk)
        out = serialize_followup(f)
        if nxt:
            out['next'] = serialize_followup(_followup_qs(request).get(pk=nxt.pk))
        return Response(out)


class ARFollowUpListView(APIView):
    """Follow-ups across accounts: mine or everyone's, by when they are due."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_ar_access(request.user):
            return _deny()
        now = timezone.now()
        qs = _followup_qs(request)
        if request.query_params.get('scope') != 'all':
            qs = qs.filter(assigned_to=request.user)
        when = request.query_params.get('when') or 'open'
        end_of_today = timezone.make_aware(datetime.combine(timezone.localdate(), time.max))
        if when == 'overdue':
            qs = qs.filter(status='pending', scheduled_at__lt=now)
        elif when == 'today':
            qs = qs.filter(status='pending', scheduled_at__gte=now, scheduled_at__lte=end_of_today)
        elif when == 'upcoming':
            qs = qs.filter(status='pending', scheduled_at__gt=end_of_today)
        elif when == 'done':
            qs = qs.filter(status='done').order_by('-done_at')
        else:
            qs = qs.filter(status='pending')
        qs = qs[:300]
        return Response({'results': [serialize_followup(f, now, with_account=True) for f in qs]})


class ARAssigneesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_ar_access(request.user):
            return _deny()
        company = getattr(request.user, 'company', None)
        cid = request.query_params.get('company_id')
        if cid and is_platform_admin(request.user):
            from companies.models import Company
            company = Company.objects.filter(pk=cid).first()
        if not company:
            return Response({'results': [{'id': request.user.id, 'name': request.user.name}]})
        return Response({'results': [{'id': u.id, 'name': u.name} for u in ar_users(company)]})
