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


def _models_by_type(module=''):
    """target_type → (model, select_related). Club 1000 has its own leads and
    follow-ups, so the module decides which model a "lead" line points at."""
    from sales.models import Lead, FollowUp, SiteVisit, Booking, Closure, Plot, Project, LeadTransfer
    from receivables.models import ARAccount
    from club1000 import models as c1k
    from attendance.models import LeaveApplication
    common = {
        'investor': (c1k.Investor, ()), 'payout': (c1k.Payout, ('investor',)),
        'referral-reward': (c1k.ReferralReward, ('investor',)), 'scheme': (c1k.Scheme, ()),
        'leave-action': (LeaveApplication, ('user',)), 'leave': (LeaveApplication, ('user',)),
        'ar_account': (ARAccount, ('booking', 'booking__project', 'booking__plot')),
    }
    if module == 'Club 1000':
        return {**common, 'lead': (c1k.Lead, ()), 'follow-up': (c1k.FollowUp, ('lead',))}
    return {
        **common,
        'lead': (Lead, ()), 'follow-up': (FollowUp, ('lead',)), 'site-visit': (SiteVisit, ('lead',)),
        'booking': (Booking, ('project', 'plot')), 'closure': (Closure, ('project',)),
        'plot': (Plot, ('project',)), 'project': (Project, ()),
        'lead-transfer': (LeadTransfer, ('lead',)),
    }


def _labels(rows):
    """Name each log line's record (older lines were written before names were
    stored with them) and, for anything on a Sales lead, give the lead's id so the
    screen can open it. One query per record type and module on the page."""
    from .changes import record_label
    want = {}
    for r in rows:
        if r['target_id'].isdigit() and (not r.get('label') or r['target_type'] in LEAD_TYPES):
            scope = 'Club 1000' if r['module'] == 'Club 1000' else ''
            want.setdefault((scope, r['target_type']), set()).add(int(r['target_id']))
    found = {}
    for (scope, ttype), ids in want.items():
        spec = _models_by_type(scope).get(ttype)
        if not spec:
            continue
        model, rel = spec
        try:
            for obj in model._base_manager.filter(pk__in=ids).select_related(*rel):
                lead_id = None
                if not scope and ttype in LEAD_TYPES:
                    lead_id = obj.pk if ttype == 'lead' else getattr(obj, 'lead_id', None)
                found[(scope, ttype, str(obj.pk))] = (record_label(obj), lead_id)
        except Exception:
            continue
    for r in rows:
        scope = 'Club 1000' if r['module'] == 'Club 1000' else ''
        label, lead_id = found.get((scope, r['target_type'], r['target_id']), ('', None))
        if not r.get('label'):
            r['label'] = label
        r['lead_id'] = lead_id
    return rows


LEAD_TYPES = ('lead', 'follow-up', 'site-visit', 'lead-transfer')


def serialize(row):
    try:
        details = json.loads(row.details) if row.details else {}
    except ValueError:
        details = {}
    return {
        'label': details.get('label', ''),
        'changes': details.get('changes') or [],
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
            # One module's Log tab may cover several names (Sales + Channel Partner).
            qs = qs.filter(module__in=[m.strip() for m in p['module'].split(',') if m.strip()])
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
        # The filter lists are only needed with the first page.
        modules = sorted(set(qs.order_by().values_list('module', flat=True).distinct()[:50])) if not record and page == 1 else []
        actors = []
        if is_log_admin(u) and not record and page == 1:
            from django.db.models import Q
            from accounts.models import User
            # Everyone in the company can be picked, not only people already in the log
            # (plus anyone in the log who has since left or moved company).
            ids = set(qs.order_by().exclude(actor__isnull=True).values_list('actor_id', flat=True).distinct()[:500])
            cid = p.get('company_id') if is_platform_admin(u) else u.company_id
            people = Q(id__in=ids)
            if cid:
                people |= Q(company_id=cid, is_active=True)
            elif u.company_id:
                people |= Q(company_id=u.company_id, is_active=True)
            actors = [{'id': uid, 'name': name or ''} for uid, name in
                      User.objects.filter(people).order_by('name').values_list('id', 'name')[:1000]]
        results = _labels([serialize(r) for r in chunk[:PAGE]])
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
