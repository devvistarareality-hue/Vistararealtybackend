import json
from datetime import datetime, time

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import is_platform_admin
from receivables.services import parse_date
from .models import ActivityLog

PAGE = 50


def is_log_admin(user):
    """Company admins and platform staff read the whole company's log."""
    return bool(is_platform_admin(user) or user.is_staff or getattr(user, 'role', '') == 'Admin')


def serialize(row):
    try:
        details = json.loads(row.details) if row.details else {}
    except ValueError:
        details = {}
    return {
        'id': row.id,
        'at': row.created_at.isoformat(),
        'actor': {'id': row.actor_id, 'name': row.actor_name or ''},
        'module': row.module,
        'action': row.action,
        'target_type': row.target_type,
        'target_id': row.target_id,
        'summary': row.summary or '',
        'details': details,
        'method': row.method,
    }


def _booking_legacy(company_id, booking_id, logged_actions):
    """What a booking's own fields already record — who submitted, approved,
    rejected or cancelled it — for the steps the log itself does not have (they
    happened before the log existed)."""
    from sales.models import Booking
    b = (Booking.objects.filter(pk=booking_id, company_id=company_id)
         .select_related('stm', 'approved_by', 'rejected_by', 'accounts_approved_by',
                         'accounts_rejected_by', 'cancelled_by').first()) if booking_id.isdigit() else None
    if not b:
        return []
    out = []

    def add(key, module, at, who, summary):
        if at and key not in logged_actions:
            out.append({'id': 'b%s-%s' % (b.id, key), 'at': at.isoformat(), 'actor': {'id': None, 'name': who or ''},
                        'module': module, 'action': key.split(':')[0], 'target_type': 'booking',
                        'target_id': str(b.id), 'summary': summary, 'details': {}, 'method': '', 'legacy': True})

    stm = b.manual_stm_name or (b.stm.name if b.stm_id else '')
    add('submitted:Sales', 'Sales', getattr(b, 'created_at', None), stm, 'Submitted booking')
    add('approved:Sales', 'Sales', b.approved_at, b.approved_by.name if b.approved_by_id else '', 'Approved booking')
    add('approved:Accounts & Finance', 'Accounts & Finance', b.accounts_approved_at,
        b.accounts_approved_by.name if b.accounts_approved_by_id else '', 'Approved (Accounts) booking')
    add('rejected:Accounts & Finance', 'Accounts & Finance', b.accounts_rejected_at,
        b.accounts_rejected_by.name if b.accounts_rejected_by_id else '',
        'Rejected (Accounts) booking' + ((' — ' + b.accounts_rejected_reason) if b.accounts_rejected_reason else ''))
    if not b.accounts_rejected_at:
        add('rejected:Sales', 'Sales', b.rejected_at, b.rejected_by.name if b.rejected_by_id else '', 'Rejected booking')
    add('cancelled:Sales', 'Sales', b.cancelled_at, b.cancelled_by.name if b.cancelled_by_id else '', 'Cancelled booking')
    return out


class ActivityLogView(APIView):
    """The activity log. Admins see everyone in their company (platform admins any
    company); everyone else sees what they did themselves. The history of one
    record (?target_type=booking&target_id=12) is open to anyone in its company."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        p = request.query_params
        qs = ActivityLog.objects.all()
        if is_platform_admin(u):
            if p.get('company_id'):
                qs = qs.filter(company_id=p['company_id'])
        else:
            qs = qs.filter(company_id=u.company_id)
        record = p.get('target_type') and p.get('target_id')
        if record:
            types = [t for t in p['target_type'].split(',') if t]
            qs = qs.filter(target_type__in=types, target_id=p['target_id'])
        elif not is_log_admin(u):
            qs = qs.filter(actor=u)
        if p.get('module'):
            qs = qs.filter(module=p['module'])
        if p.get('actor'):
            qs = qs.filter(actor_id=p['actor'])
        if p.get('action'):
            qs = qs.filter(action=p['action'])
        d_from, d_to = parse_date(p.get('from')), parse_date(p.get('to'))
        if d_from:
            qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(d_from, time.min)))
        if d_to:
            qs = qs.filter(created_at__lte=timezone.make_aware(datetime.combine(d_to, time.max)))
        try:
            page = max(1, int(p.get('page') or 1))
        except ValueError:
            page = 1
        needle = (p.get('q') or '').strip().lower()
        if needle:
            # The description is encrypted, so the text search runs here, over the
            # most recent matches of the other filters.
            rows = [r for r in qs[:3000] if needle in (r.summary or '').lower() or needle in (r.actor_name or '').lower()]
            chunk = rows[(page - 1) * PAGE: page * PAGE + 1]
        else:
            chunk = list(qs[(page - 1) * PAGE: page * PAGE + 1])
        modules = sorted(set(qs.values_list('module', flat=True).distinct()[:50])) if not record else []
        actors = []
        if is_log_admin(u) and not record:
            seen = {}
            for aid, name in qs.values_list('actor_id', 'actor_name')[:2000]:
                if aid and aid not in seen:
                    seen[aid] = name
            actors = sorted(({'id': k, 'name': v or ''} for k, v in seen.items()), key=lambda a: a['name'].lower())
        results = [serialize(r) for r in chunk[:PAGE]]
        if record and 'booking' in types and page == 1 and not needle:
            logged = set()
            for r in qs.values_list('action', 'module'):
                mod = 'Sales' if r[1] == 'Channel Partner' else r[1]
                logged.add('%s:%s' % (r[0], mod))
            cid = p.get('company_id') if is_platform_admin(u) else u.company_id
            if is_platform_admin(u) and not cid:
                from sales.models import Booking
                cid = Booking.objects.filter(pk=p['target_id']).values_list('company_id', flat=True).first() if p['target_id'].isdigit() else None
            results = sorted(results + _booking_legacy(cid, p['target_id'], logged), key=lambda x: x['at'], reverse=True)
        return Response({
            'page': page,
            'has_more': len(chunk) > PAGE,
            'results': results,
            'modules': modules,
            'actors': actors,
            'can_see_all': is_log_admin(u),
        })
