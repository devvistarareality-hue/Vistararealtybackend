"""What actually changed, and which record it was.

While an API write is being handled (the activity middleware opens a capture),
every model save is compared with the row as it was in the database, so the log
can say "Status: Callback → Warm" instead of listing every field the screen
happened to send. Records are named the way people know them — "Rahul Shah
(98250 12345)", not "lead #45975".

Only saves made during a watched request are compared (one extra read per save),
and queryset .update() calls are not seen — they bypass model saves.
"""
import threading

_ctx = threading.local()

# Never shown in a change list: bookkeeping, blind indexes, secrets.
SKIP_FIELDS = {
    'updated_at', 'created_at', 'last_login', 'password', 'session_token_app', 'session_token_web',
    'reminder_sent_at', 'escalated_at', 'phone_key', 'email_key', 'otp', 'otp_code', 'otp_expires_at',
}
SKIP_MODELS = {'activitylog', 'notification', 'session', 'logentry', 'arreceiptaudit', 'userlocation',
               'otp', 'outstandingtoken', 'blacklistedtoken'}
TRACKED_APPS = {'sales', 'club1000', 'receivables', 'attendance', 'accounts', 'companies'}

# Model → the target_type the log uses (matches the URL-based names).
TYPE_OF = {
    'lead': 'lead', 'followup': 'follow-up', 'sitevisit': 'site-visit', 'booking': 'booking',
    'closure': 'closure', 'plot': 'plot', 'project': 'project', 'leadsource': 'source',
    'channelpartner': 'channel-partner', 'user': 'user', 'leadtransfer': 'lead-transfer',
    'araccount': 'ar_account', 'arreceipt': 'ar_account', 'arfollowup': 'ar_account',
    'investor': 'investor', 'payout': 'payout', 'referralreward': 'referral-reward', 'scheme': 'scheme',
    'leaveapplication': 'leave',
    'designation': 'designation', 'dashboarddefinition': 'dashboard',
}

MAX_VALUE = 80


def start():
    _ctx.changes = {}


def stop():
    out = getattr(_ctx, 'changes', None)
    _ctx.changes = None
    return list(out.values()) if out else []


def active():
    return getattr(_ctx, 'changes', None) is not None


def _short(v):
    s = '' if v is None else str(v)
    s = ' '.join(s.split())
    return (s[:MAX_VALUE] + '…') if len(s) > MAX_VALUE else s


def record_label(obj):
    """How people know this record."""
    if obj is None:
        return ''
    name = obj._meta.model_name
    try:
        if name == 'dashboarddefinition':
            return '%s · %s %s' % (obj.name or '—', obj.module or '', obj.role or '')
        if name == 'designation':
            return '%s · %s' % (obj.name or '—', obj.module or '')
        if name == 'lead':
            return '%s (%s)' % (obj.name or '—', obj.phone or '—')
        if name == 'investor':
            return '%s%s' % (obj.name or '—', (' · LOI %s' % obj.loi_no) if getattr(obj, 'loi_no', '') else '')
        if name in ('payout', 'referralreward') and getattr(obj, 'investor_id', None):
            return record_label(obj.investor)
        if name == 'leaveapplication':
            span = obj.from_date.strftime('%d %b') if obj.from_date else ''
            if obj.to_date and obj.to_date != obj.from_date:
                span += ' – ' + obj.to_date.strftime('%d %b')
            return '%s · %s %s' % (obj.user.name if obj.user_id else '—', obj.get_leave_type_display(), span)
        if name in ('followup', 'sitevisit', 'leadtransfer') and getattr(obj, 'lead_id', None):
            return record_label(obj.lead)
        if name == 'booking':
            unit = obj.plot_numbers or (obj.plot.number if obj.plot_id else '')
            return '%s · %s %s' % (obj.client_name or '—', obj.project.name if obj.project_id else '', unit)
        if name == 'closure':
            return '%s · %s' % (obj.client_name or '—', obj.project.name if obj.project_id else '')
        if name == 'plot':
            return '%s Plot %s' % (obj.project.name if obj.project_id else '', obj.number)
        if name in ('araccount',):
            return record_label(obj.booking)
        if name in ('arreceipt', 'arfollowup'):
            return record_label(obj.account.booking)
        for attr in ('name', 'client_name', 'title', 'number', 'email'):
            v = getattr(obj, attr, None)
            if v:
                return str(v)
    except Exception:
        pass
    return ''


def _display(obj, field, value):
    """A field value the way a person reads it."""
    if value in (None, '', [], {}):
        return '—'
    if field.choices:
        return str(dict(field.flatchoices).get(value, value))
    if field.is_relation and field.many_to_one:
        try:
            rel = field.related_model._base_manager.filter(pk=value).first()
            return _short(record_label(rel) or value)
        except Exception:
            return str(value)
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (list, tuple)):
        return _short(', '.join(str(x) for x in value))
    return _short(value)


def _tracked(sender):
    m = sender._meta
    return m.app_label in TRACKED_APPS and m.model_name not in SKIP_MODELS


def on_pre_save(sender, instance, raw=False, **kwargs):
    if raw or not active() or not _tracked(sender):
        return
    try:
        key = (sender._meta.app_label, sender._meta.model_name, instance.pk)
        if instance.pk is None:
            instance._activity_new = True
            return
        old = sender._base_manager.filter(pk=instance.pk).first()
        if old is None:
            instance._activity_new = True
            return
        diffs = []
        for f in sender._meta.concrete_fields:
            if f.primary_key or f.name in SKIP_FIELDS or f.name.endswith('_key') \
                    or getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False):
                continue
            a, b = getattr(old, f.attname), getattr(instance, f.attname)
            if a == b or (a in (None, '') and b in (None, '')):
                continue
            if hasattr(a, 'normalize') and hasattr(b, 'normalize'):   # Decimals: 5.0 == 5.00
                try:
                    if a == b:
                        continue
                except Exception:
                    pass
            diffs.append({'field': str(f.verbose_name).capitalize(), 'from': _display(old, f, a),
                          'to': _display(instance, f, b)})
        entry = _ctx.changes.get(key)
        if entry is None:
            _ctx.changes[key] = {'model': sender._meta.model_name, 'type': TYPE_OF.get(sender._meta.model_name, sender._meta.model_name),
                                 'id': instance.pk, 'label': record_label(instance), 'created': False, 'changes': diffs}
        else:
            # Saved twice in one request: merge, keeping the first "from".
            seen = {d['field']: d for d in entry['changes']}
            for d in diffs:
                if d['field'] in seen:
                    seen[d['field']]['to'] = d['to']
                else:
                    entry['changes'].append(d)
            entry['changes'] = [d for d in entry['changes'] if d['from'] != d['to']]
    except Exception:
        pass


def on_post_save(sender, instance, created=False, raw=False, **kwargs):
    if raw or not active() or not _tracked(sender):
        return
    if created or getattr(instance, '_activity_new', False):
        try:
            key = (sender._meta.app_label, sender._meta.model_name, instance.pk)
            _ctx.changes.setdefault(key, {'model': sender._meta.model_name,
                                          'type': TYPE_OF.get(sender._meta.model_name, sender._meta.model_name),
                                          'id': instance.pk, 'label': record_label(instance), 'created': True, 'changes': []})
        except Exception:
            pass


def on_post_delete(sender, instance, **kwargs):
    if not active() or not _tracked(sender):
        return
    try:
        key = (sender._meta.app_label, sender._meta.model_name, instance.pk)
        _ctx.changes[key] = {'model': sender._meta.model_name, 'type': TYPE_OF.get(sender._meta.model_name, sender._meta.model_name),
                             'id': instance.pk, 'label': record_label(instance), 'created': False, 'deleted': True, 'changes': []}
    except Exception:
        pass


def change_text(changes, limit=3):
    """"Status: Callback → Warm; STM remarks changed" — at most `limit` fields."""
    parts = []
    for d in changes[:limit]:
        if len(d['from']) > 30 or len(d['to']) > 30:
            parts.append('%s changed' % d['field'])
        else:
            parts.append('%s: %s → %s' % (d['field'], d['from'], d['to']))
    if len(changes) > limit:
        parts.append('+%d more' % (len(changes) - limit))
    return '; '.join(parts)
