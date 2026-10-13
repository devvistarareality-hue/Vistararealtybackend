"""Recover Meta's lead id (leadgen_id) for recent form leads saved before it was kept.

    python manage.py meta_backfill_leadgen --days 30 [--dry-run]

For every lead form on the connected Pages, Meta lists its leads (id, created time,
phone). Each is matched to the Nexora lead from the same form with the same phone
(last 10 digits) and no id yet, and the id is filled in. Uses .update(), so no stage
events are sent for the past — only stages that happen from now on are reported.
"""
from datetime import timedelta

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from sales.models import Lead, MetaWebhookConfig

GRAPH = 'https://graph.facebook.com/v26.0'


def _last10(raw):
    d = ''.join(c for c in str(raw or '') if c.isdigit())
    return d[-10:]


class Command(BaseCommand):
    help = 'Fill in Meta lead ids (leadgen_id) for recent Meta form leads.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, days=30, dry_run=False, **opts):
        since = int((timezone.now() - timedelta(days=days)).timestamp())
        total = 0
        for cfg in MetaWebhookConfig.objects.exclude(page_access_token=''):
            pages = requests.get(f'{GRAPH}/me/accounts', params={'access_token': cfg.page_access_token, 'limit': 100,
                                                                'fields': 'id,access_token'}, timeout=20).json()
            tokens = {p['id']: p.get('access_token') for p in pages.get('data', [])}
            for page in cfg.pages_data or []:
                ptok = tokens.get(str(page.get('page_id')))
                if not ptok:
                    continue
                for form in page.get('forms', []):
                    fid = str(form.get('id'))
                    waiting = {}
                    for lid, ph in Lead.objects.filter(company_id=cfg.company_id, meta_form_id=fid,
                                                       meta_leadgen_id='').values_list('id', 'phone'):
                        if _last10(ph):
                            waiting.setdefault(_last10(ph), []).append(lid)
                    if not waiting:
                        continue
                    url = f'{GRAPH}/{fid}/leads'
                    params = {'access_token': ptok, 'fields': 'id,created_time,field_data', 'limit': 200,
                              'filtering': f'[{{"field":"time_created","operator":"GREATER_THAN","value":{since}}}]'}
                    found = 0
                    while url:
                        r = requests.get(url, params=params, timeout=30).json()
                        if 'error' in r:
                            self.stdout.write(f"  form {fid}: {r['error'].get('message', '')[:120]}")
                            break
                        for m in r.get('data', []):
                            phone = next((f['values'][0] for f in m.get('field_data', [])
                                          if f.get('name') in ('phone_number', 'phone') and f.get('values')), '')
                            ids = waiting.get(_last10(phone))
                            if ids:
                                lead_id = ids.pop(0)
                                if not dry_run:
                                    Lead.objects.filter(pk=lead_id, meta_leadgen_id='').update(meta_leadgen_id=m['id'])
                                found += 1
                        url = r.get('paging', {}).get('next')
                        params = None
                    if found:
                        self.stdout.write(f"  {page.get('page_name')} · {form.get('name')}: {found}")
                    total += found
        self.stdout.write(f"{'Would fill' if dry_run else 'Filled'} {total} lead id(s).")
