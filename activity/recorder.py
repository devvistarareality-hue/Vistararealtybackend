"""Records every change made through the API into the activity log.

The middleware sees each successful POST / PUT / PATCH / DELETE under /api/ and
writes one ActivityLog row: who, which module, which record, what was done. A view
can describe its change better than a URL can ("Approved booking — Asha · Kalrav
Plot 12") by calling `note(request, …)`; otherwise a description is built from
the URL and method ("Updated lead #52 (status, remarks)").

Logging never breaks a request: any failure here is swallowed and logged.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

WRITE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
# Housekeeping calls that are not anyone "doing" something: signing in, token
# refresh, reading notifications, autosaves, uploads, searches, webhooks.
SKIP = re.compile(
    r'^/api/(auth/(login|otp|token|notifications)|sales/(webhooks|media|leads/search|bookings/draft)'
    r'|.*/(search|preview)/?$)')
MODULES = {
    'sales': 'Sales', 'ar': 'AR', 'club1000': 'Club 1000', 'attendance': 'HR',
    'auth': 'Admin', 'company': 'Admin', 'activity': 'Admin',
}
VERBS = {
    'approve': 'Approved', 'reject': 'Rejected', 'cancel': 'Cancelled', 'withdraw': 'Withdrew',
    'created': 'Created', 'updated': 'Updated', 'deleted': 'Deleted', 'discard': 'Discarded',
    'mark-paid': 'Marked paid', 'redeem': 'Redeemed', 'renew': 'Renewed', 'revise': 'Revised',
    'done': 'Completed', 'cancelled': 'Cancelled',
}
# Request fields worth keeping the value of (short, not personal). Everything else
# is recorded by name only.
KEEP_VALUES = {'action', 'status', 'stm_status', 'approval_status', 'reason', 'remarks_reason',
               'channel', 'mode', 'is_active', 'role'}
SECRET = re.compile(r'pass|otp|token|secret|pin$|aadhaar|pan_|account_no|ifsc', re.I)


def note(request, summary, action=None, target_type=None, target_id=None, module=None, details=None):
    """Describe the change this request made; the middleware writes it."""
    raw = getattr(request, '_request', request)
    raw._activity = {k: v for k, v in {
        'summary': summary, 'action': action, 'target_type': target_type,
        'target_id': target_id, 'module': module, 'details': details}.items() if v not in (None, '')}


def skip(request):
    """This request changed nothing worth logging (a preview, a dry run)."""
    getattr(request, '_request', request)._activity_skip = True


def _singular(word):
    if word.endswith('ies'):
        return word[:-3] + 'y'
    if word.endswith('s') and not word.endswith('ss'):
        return word[:-1]
    return word


def _human(word):
    return word.replace('-', ' ').replace('_', ' ')


def describe(method, path, body, response_id=None):
    """(module, action, target_type, target_id, summary) from the URL alone."""
    parts = [p for p in path.split('/') if p][1:]   # drop "api"
    mod_key = parts[0] if parts else ''
    module = MODULES.get(mod_key, mod_key.title())
    segs = parts[1:]
    if 'accounts-action' in segs:
        module = 'Accounts & Finance'
    elif 'channel-partners' in segs:
        module = 'Channel Partner'
    ids = [i for i, s in enumerate(segs) if s.isdigit()]
    target_id = segs[ids[0]] if ids else (str(response_id) if response_id else '')
    if ids:
        resource = segs[ids[0] - 1] if ids[0] > 0 else mod_key
        sub = segs[ids[0] + 1] if len(segs) > ids[0] + 1 else ''
    else:
        resource = segs[0] if segs else mod_key
        sub = segs[1] if len(segs) > 1 else ''
    body_action = str((body or {}).get('action') or '').lower() if isinstance(body, dict) else ''
    if sub in ('action', 'accounts-action') and body_action:
        sub = body_action
    elif not sub and body_action in VERBS and method in ('POST', 'PATCH'):
        sub = body_action    # e.g. attendance/leave-action/<id>/ {"action": "approve"}
    target_type = _singular(resource)
    label = _human(target_type)
    ref = (' #%s' % target_id) if target_id else ''
    if sub:
        action = sub
        verb = VERBS.get(sub)
        summary = ('%s %s%s' % (verb, label, ref)) if verb else ('%s · %s%s' % (_human(sub).capitalize(), label, ref))
    elif method == 'DELETE':
        action, summary = 'deleted', 'Deleted %s%s' % (label, ref)
    elif method in ('PATCH', 'PUT'):
        action = 'updated'
        fields = [k for k in (body or {}) if isinstance(body, dict)][:8] if isinstance(body, dict) else []
        summary = 'Updated %s%s%s' % (label, ref, (' (%s)' % ', '.join(_human(f) for f in fields)) if fields else '')
    elif not resource.endswith('s') and not target_id:
        # An endpoint named after what it does: sign-in, apply-leave, distribute…
        action, summary = resource, _human(resource).capitalize()
    else:
        action, summary = 'created', 'Created %s%s' % (label, ref)
    return module, action, target_type, target_id, summary


def _body_details(body):
    if not isinstance(body, dict):
        return {}
    out = {'fields': sorted(k for k in body if not SECRET.search(k))[:40]}
    kept = {k: str(v)[:200] for k, v in body.items()
            if k in KEEP_VALUES and not SECRET.search(k) and isinstance(v, (str, int, float, bool))}
    if kept:
        out['values'] = kept
    return out


# A status change that is really a decision reads as that decision.
STATUS_VERBS = {'approved': 'Approved', 'rejected': 'Rejected', 'cancelled': 'Cancelled', 'canceled': 'Cancelled',
                'completed': 'Completed', 'done': 'Completed', 'withdrawn': 'Withdrew'}


def ref_in(summary, tid):
    return bool(tid) and ('#%s' % tid) in (summary or '')


PLOT_VERBS = {'hold': 'Held', 'release': 'Released', 'cancel-hold': 'Cancelled hold on', 'bulk': 'Added',
              'bulk-delete': 'Deleted', 'rename-type': 'Renamed type of'}


def plot_names(ids):
    """"Kalrav 2 · Plot 12, 13 · Pratishtha · Plot 4" for a list of plot ids."""
    try:
        from sales.models import Plot
        rows = (Plot.objects.filter(id__in=ids).select_related('project')
                .only('id', 'number', 'project__name').order_by('project__name', 'id'))
        groups = {}
        for pl in rows:
            groups.setdefault(pl.project.name if pl.project_id else '', []).append(str(pl.number))
        return ' · '.join('%s Plot %s' % (proj, ', '.join(nums)) if proj else 'Plot ' + ', '.join(nums)
                          for proj, nums in groups.items())
    except Exception:
        return ''


def _new_plots(body):
    """"Audit Tower Plot A1, A2" for a bulk create (the plots don't exist yet)."""
    try:
        from sales.models import Project
        proj = Project.objects.filter(pk=body.get('project_id')).values_list('name', flat=True).first() or ''
        nums = [str(p.get('number')) for p in body.get('plots') if isinstance(p, dict) and p.get('number')]
        shown = ', '.join(nums[:30]) + (' +%d more' % (len(nums) - 30) if len(nums) > 30 else '')
        return '%d plot%s — %s Plot %s' % (len(nums), '' if len(nums) == 1 else 's', proj, shown)
    except Exception:
        return ''


def _plot_ids(body):
    if not isinstance(body, dict):
        return []
    raw = body.get('plot_ids') or ([body['plot_id']] if body.get('plot_id') else [])
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    return [int(x) for x in raw if str(x).isdigit()][:200]


def _client_ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR', '')) or ''


class ActivityLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        body = None
        watch = request.method in WRITE_METHODS and request.path.startswith('/api/') and not SKIP.match(request.path)
        if watch:
            try:
                ctype = request.META.get('CONTENT_TYPE', '')
                size = int(request.META.get('CONTENT_LENGTH') or 0)
                # Read JSON bodies up front (Django caches them, so the view still can).
                # Uploads are never read here.
                if 'json' in ctype and 0 < size <= 65536:
                    body = json.loads(request.body or b'{}')
            except Exception:
                body = None
        from . import changes as ch
        plots = ''
        if watch and '/plots/' in request.path:
            ids = _plot_ids(body)
            if ids:
                plots = plot_names(ids)
            elif request.path.endswith('/plots/bulk/') and isinstance(body, dict) and body.get('plots'):
                plots = _new_plots(body)
        if watch:
            ch.start()
        try:
            response = self.get_response(request)
        finally:
            captured = ch.stop() if watch else []
        if watch:
            try:
                self._record(request, response, body, captured, plots)
            except Exception:
                logger.exception('activity log failed for %s %s', request.method, request.path)
        return response

    def _record(self, request, response, body, captured=(), plots=''):
        if response.status_code >= 400 or getattr(request, '_activity_skip', False):
            return
        user = getattr(request, 'user', None)
        if not (user and getattr(user, 'is_authenticated', False)):
            return
        response_id = None
        if request.method == 'POST' and 'json' in (response.get('Content-Type') or ''):
            try:
                data = json.loads(response.content or b'{}')
                if isinstance(data, dict) and isinstance(data.get('id'), int):
                    response_id = data['id']
            except Exception:
                pass
        module, action, ttype, tid, summary = describe(request.method, request.path, body, response_id)
        extra = getattr(request, '_activity', None) or {}
        if plots and not extra.get('summary'):
            # Name the units: "Held Kalrav 2 Plot 12, 13", not "Hold · plot".
            sub = [x for x in request.path.split('/') if x][-1]
            verb = PLOT_VERBS.get(sub) or ('Updated' if request.method in ('PATCH', 'PUT') else _human(sub).capitalize())
            extra = {**extra, 'summary': '%s %s' % (verb, plots), 'target_type': 'plot'}
            if len(_plot_ids(body)) == 1:
                extra['target_id'] = _plot_ids(body)[0]
        details = _body_details(body)
        if extra.get('details'):
            details['info'] = extra['details']
        # What the saves in this request actually changed. The record the URL points
        # at comes first; anything else that changed alongside it is kept as well.
        from .changes import change_text
        primary = next((c for c in captured if c['type'] == ttype and str(c['id']) == str(tid)), None) \
            or next((c for c in captured if c['type'] == ttype), None) or (captured[0] if captured else None)
        if primary:
            if not tid:
                tid = str(primary['id'])
            label = primary.get('label') or ''
            if label:
                details['label'] = label
            details['changes'] = primary['changes']
            others = [c for c in captured if c is not primary and (c['changes'] or c['created'] or c.get('deleted'))]
            if others:
                details['also'] = [{'type': c['type'], 'id': c['id'], 'label': c['label'], 'changes': c['changes'],
                                    'created': c['created'], 'deleted': c.get('deleted', False)} for c in others[:10]]
            if request.method in ('PATCH', 'PUT') and not primary['created'] and not primary['changes'] and not others \
                    and not extra:
                return   # saved, but nothing changed — not worth a line
            if not extra.get('summary'):
                noun = _human(primary['type'])
                who = (' — ' + label) if label else (' #%s' % primary['id'])
                same = [c for c in captured if c['type'] == primary['type']
                        and c['created'] == primary['created'] and c.get('deleted') == primary.get('deleted')]
                status_to = next((d['to'] for d in primary['changes'] if d['field'] == 'Status'), '')
                status_verb = STATUS_VERBS.get(status_to.strip().lower())
                if len(same) > 1 and (primary['created'] or primary.get('deleted')):
                    # Many at once (bulk delete, import): one line naming them.
                    names = [c['label'] for c in same if c['label']]
                    summary = '%s %d %ss — %s%s' % ('Deleted' if primary.get('deleted') else 'Created', len(same), noun,
                                                   ', '.join(names[:8]), ' +%d more' % (len(names) - 8) if len(names) > 8 else '')
                    details['all'] = [c['label'] for c in same][:200]
                elif primary.get('deleted'):
                    summary = 'Deleted %s%s' % (noun, who)
                elif primary['created'] and 'apply-leave' in request.path:
                    summary = 'Applied for leave%s' % who
                elif primary['created']:
                    summary = 'Created %s%s' % (noun, who)
                elif status_verb and action in ('updated', 'approve', 'reject', 'cancel'):
                    rest = [d for d in primary['changes'] if d['field'] != 'Status']
                    summary = '%s %s%s%s' % (status_verb, noun, who, (' · ' + change_text(rest)) if rest else '')
                elif primary['changes'] and action == 'updated':
                    summary = 'Updated %s%s · %s' % (noun, who, change_text(primary['changes']))
                elif label and ref_in(summary, tid):
                    summary = summary.replace('#%s' % tid, '— %s' % label, 1)
                    if primary['changes']:
                        summary += ' · ' + change_text(primary['changes'])
        from .models import ActivityLog
        ActivityLog.objects.create(
            company_id=getattr(user, 'company_id', None),
            actor=user,
            actor_name=getattr(user, 'name', '') or getattr(user, 'email', '') or '',
            module=(extra.get('module') or module)[:30],
            action=(extra.get('action') or action)[:30],
            target_type=(extra.get('target_type') or ttype)[:40],
            target_id=str(extra.get('target_id') or tid)[:40],
            summary=extra.get('summary') or summary,
            details=json.dumps(details, default=str) if details else '',
            method=request.method,
            path=request.path[:255],
            status_code=response.status_code,
            ip=_client_ip(request),
        )
