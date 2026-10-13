"""Send lead stages back to Meta (Conversions API for CRM — "conversion leads").

When a Meta form lead moves on in Nexora, Meta is told the same day, so it can find
more people who actually come for a site visit rather than people who only fill forms.

Stages (the event names mapped in Events Manager → the CRM dataset):
  Lead_Received          a Meta form lead arrives
  Qualified              the telecaller marks it warm (passes it to the STM)
  Site_Visit_Scheduled   a site visit is booked
  Site_Visit_Done        the site visit is completed
  Booked                 a booking on the lead is approved (sold)
  Disqualified           marked not qualified / not interested

Each stage is sent once per lead (MetaCapiEvent is unique per lead + stage). Only Meta
leads are reported (a leadgen id or a Meta form id). Sending happens after the save has
committed, on a background thread, so no screen waits on Meta; a failure is kept with
Meta's reply and retried by `manage.py meta_capi_retry`. Nothing is sent until the
company switches it on in Lead Setup with its dataset id and token.

Personal data never leaves in the clear: phone (with 91) and email are SHA-256 hashed,
as Meta requires.
"""
import hashlib
import json
import logging
import threading

from django.conf import settings
from django.db import connection, transaction
from django.db.models.signals import post_save, pre_save
from django.utils import timezone

logger = logging.getLogger(__name__)

GRAPH = 'https://graph.facebook.com/v26.0'  # Meta's CRM guide: latest API version
EVENTS = ('Lead_Received', 'Qualified', 'Site_Visit_Scheduled', 'Site_Visit_Done', 'Booked', 'Disqualified')
DISQUALIFIED = ('not_qualified', 'not_interested')
MAX_ATTEMPTS = 5


def _sha(v):
    return hashlib.sha256(v.encode('utf-8')).hexdigest()


def _phone(raw):
    digits = ''.join(c for c in str(raw or '') if c.isdigit())
    if len(digits) == 10:
        digits = '91' + digits
    return digits if len(digits) >= 11 else ''


def _config(company_id):
    from .models import MetaWebhookConfig
    cfg = MetaWebhookConfig.objects.filter(company_id=company_id).first()
    if cfg and cfg.capi_enabled and cfg.capi_dataset_id and cfg.capi_token:
        return cfg
    return None


def _enabled(company_id):
    """Cheap check for the save hooks (cached a minute; Lead Setup clears it)."""
    from django.core.cache import cache
    key = f'capi_on_{company_id}'
    on = cache.get(key)
    if on is None:
        on = bool(_config(company_id))
        cache.set(key, on, 60)
    return on


def clear_cache(company_id):
    from django.core.cache import cache
    cache.delete(f'capi_on_{company_id}')


def is_meta_lead(lead):
    return bool(lead and (lead.meta_leadgen_id or lead.meta_form_id))


def payload(lead, event_name, when, test_code=''):
    user = {}
    if lead.meta_leadgen_id and lead.meta_leadgen_id.isdigit():
        user['lead_id'] = int(lead.meta_leadgen_id)
    ph = _phone(lead.phone)
    if ph:
        user['ph'] = [_sha(ph)]
    em = (lead.email or '').strip().lower()
    if em:
        user['em'] = [_sha(em)]
    body = {'data': [{
        'event_name': event_name,
        'event_time': int(when.timestamp()),
        'action_source': 'system_generated',
        'user_data': user,
        'custom_data': {'event_source': 'crm', 'lead_event_source': 'Vistara CRM'},
    }]}
    if test_code:
        body['test_event_code'] = test_code
    return body


def post(cfg, body):
    """POST one payload to the dataset; returns (ok, reply text)."""
    import requests
    try:
        r = requests.post(f'{GRAPH}/{cfg.capi_dataset_id}/events', params={'access_token': cfg.capi_token},
                          json=body, timeout=15)
        text = r.text[:1000]
        return (r.status_code == 200 and '"events_received"' in text), text
    except Exception as e:                       # network trouble: keep it, retry later
        return False, f'{type(e).__name__}: {e}'[:1000]


def send(event_id):
    """Send one recorded event now (used by the thread and by the retry command)."""
    from .models import MetaCapiEvent
    ev = MetaCapiEvent.objects.select_related('lead').filter(pk=event_id).first()
    if not ev or ev.status == 'sent':
        return
    cfg = _config(ev.company_id)
    if not cfg:
        MetaCapiEvent.objects.filter(pk=ev.pk).update(status='skipped', reply='Conversions API is switched off.')
        return
    ok, reply = post(cfg, payload(ev.lead, ev.event_name, ev.event_time, cfg.capi_test_code))
    MetaCapiEvent.objects.filter(pk=ev.pk).update(
        status='sent' if ok else 'failed', attempts=ev.attempts + 1, reply=reply,
        sent_at=timezone.now() if ok else None)


def _send_later(event_id):
    def run():
        try:
            send(event_id)
        except Exception:
            logger.exception('Meta CAPI: send failed for event %s', event_id)
        finally:
            if not getattr(settings, 'META_CAPI_INLINE', False):
                connection.close()
    if getattr(settings, 'META_CAPI_INLINE', False):
        run()
    else:
        threading.Thread(target=run, daemon=True).start()


def record(lead, event_name, when=None):
    """Note that `lead` reached `event_name`; sent once, after the save commits."""
    if event_name not in EVENTS or not is_meta_lead(lead) or not lead.company_id:
        return
    if not _enabled(lead.company_id):
        return
    from .models import MetaCapiEvent

    def commit():
        try:
            ev, created = MetaCapiEvent.objects.get_or_create(
                lead_id=lead.pk, event_name=event_name,
                defaults={'company_id': lead.company_id, 'event_time': when or timezone.now()})
            if created:
                _send_later(ev.pk)
        except Exception:
            logger.exception('Meta CAPI: could not record %s for lead %s', event_name, lead.pk)
    transaction.on_commit(commit)


# ── Stage changes ────────────────────────────────────────────────────────────
def _lead_pre(sender, instance, raw=False, **kw):
    if raw or not instance.pk or not is_meta_lead(instance) or not _enabled(instance.company_id):
        instance._capi_old = None
        return
    instance._capi_old = sender.objects.filter(pk=instance.pk).values('telecaller_status', 'stm_status').first()


def _lead_post(sender, instance, created=False, raw=False, **kw):
    if raw:
        return
    try:
        if created:
            record(instance, 'Lead_Received', instance.created_at)
            return
        old = getattr(instance, '_capi_old', None) or {}
        tc, stm = instance.telecaller_status or '', instance.stm_status or ''
        if tc != (old.get('telecaller_status') or ''):
            if tc in ('warm', 'hot'):
                record(instance, 'Qualified')
            elif tc in DISQUALIFIED:
                record(instance, 'Disqualified')
        if stm != (old.get('stm_status') or ''):
            if stm == 'sv_scheduled':
                record(instance, 'Site_Visit_Scheduled')
            elif stm == 'sv_done':
                record(instance, 'Site_Visit_Done')
            elif stm in DISQUALIFIED:
                record(instance, 'Disqualified')
    except Exception:
        logger.exception('Meta CAPI: lead hook failed')


def _visit_pre(sender, instance, raw=False, **kw):
    instance._capi_old = None if (raw or not instance.pk) else \
        sender.objects.filter(pk=instance.pk).values_list('status', flat=True).first()


def _visit_post(sender, instance, created=False, raw=False, **kw):
    if raw or not instance.lead_id:
        return
    try:
        old = getattr(instance, '_capi_old', None)
        if created or old != instance.status:
            if instance.status == 'completed':
                record(instance.lead, 'Site_Visit_Done', instance.visited_at or timezone.now())
            elif instance.status == 'scheduled':
                record(instance.lead, 'Site_Visit_Scheduled')
    except Exception:
        logger.exception('Meta CAPI: visit hook failed')


def _booking_pre(sender, instance, raw=False, **kw):
    instance._capi_old = None if (raw or not instance.pk) else \
        sender.objects.filter(pk=instance.pk).values_list('status', flat=True).first()


def _booking_post(sender, instance, created=False, raw=False, **kw):
    if raw or not instance.lead_id or instance.status != 'sold':
        return
    try:
        if created or getattr(instance, '_capi_old', None) != 'sold':
            record(instance.lead, 'Booked')
    except Exception:
        logger.exception('Meta CAPI: booking hook failed')


def connect():
    from .models import Booking, Lead, SiteVisit
    pre_save.connect(_lead_pre, sender=Lead, dispatch_uid='capi_lead_pre')
    post_save.connect(_lead_post, sender=Lead, dispatch_uid='capi_lead_post')
    pre_save.connect(_visit_pre, sender=SiteVisit, dispatch_uid='capi_visit_pre')
    post_save.connect(_visit_post, sender=SiteVisit, dispatch_uid='capi_visit_post')
    pre_save.connect(_booking_pre, sender=Booking, dispatch_uid='capi_booking_pre')
    post_save.connect(_booking_post, sender=Booking, dispatch_uid='capi_booking_post')


def summary(company_id):
    """For Lead Setup: counts by status over 30 days, and the latest events."""
    from datetime import timedelta
    from django.db.models import Count
    from .models import MetaCapiEvent
    qs = MetaCapiEvent.objects.filter(company_id=company_id)
    since = timezone.now() - timedelta(days=30)
    counts = {r['event_name'] + ':' + r['status']: r['n'] for r in
              qs.filter(created_at__gte=since).values('event_name', 'status').annotate(n=Count('id'))}
    by_event = [{'event': e, 'sent': counts.get(e + ':sent', 0), 'failed': counts.get(e + ':failed', 0),
                 'pending': counts.get(e + ':pending', 0)} for e in EVENTS]
    recent = []
    for ev in qs.select_related('lead').order_by('-id')[:20]:
        try:
            msg = json.loads(ev.reply).get('error', {}).get('message', '') if ev.status == 'failed' else ''
        except ValueError:
            msg = ev.reply[:200]
        recent.append({'event': ev.event_name, 'status': ev.status, 'lead': ev.lead.name if ev.lead_id else '',
                       'lead_id': ev.lead_id, 'at': ev.created_at.isoformat(), 'error': msg})
    return {'by_event': by_event, 'recent': recent}
