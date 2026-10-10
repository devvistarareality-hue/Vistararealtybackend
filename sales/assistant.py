"""Ask Nexora — an AI assistant that answers questions from the asker's own data.

How it reads data: one flexible tool, `query_records`, that Claude fills in for any
question (which records, which filters, grouped by what, counted or summed, or a
short list). The tool does not query tables directly. It calls the real list view
(Leads, Site Visits, Follow-Ups, Closures, Bookings) in-process as the asker, and
the view hands back its already-scoped queryset (see views._ai_capture) — so every
answer is limited by exactly the same company, role, reporting-tree, project and
Sales/CP rules as the screens. It is read-only, row-capped, and encrypted fields
(names, phones, amounts) are decrypted on read and filtered/summed in Python.

Answers take several model calls, longer than one web request may run, so a
question runs as a background job (like the Excel exports): POST /api/ai/ask/
starts it, GET /api/ai/ask/<job>/ returns the answer when ready.

Model: Claude Opus 5.5 with server-side refusal fallbacks. Each question is
recorded in the Activity Log with its token use and approximate cost.
"""
import json
import os
import threading
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

MODEL = 'claude-opus-5-5'
EFFORT = 'medium'
MAX_ROUNDS = 8            # model calls per question (each may run several queries)
LIST_LIMIT = 100          # rows a list may return to the model
SCAN_LIMIT = 20000        # rows read when filtering/summing encrypted fields
DAILY_LIMIT = 60          # questions per person per day
JOB_DIR = os.path.join('/tmp', 'nexora-ai')
# Opus 5.5 list prices, USD per million tokens, and a rupee rate for the log.
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 4.0, 20.0, 0.20, 5.0
USD_INR = 85.0


def can_use_ai(user):
    """Granted per person in User Management (can_use_ai); real admins always."""
    from .views import _is_hard_admin
    return bool(user and user.is_authenticated
                and (_is_hard_admin(user) or getattr(user, 'can_use_ai', False)))


# ── What can be asked about ──────────────────────────────────────────────────
# Per entity: the list view that scopes it, and its fields as
#   alias: (orm path, kind)   kind: text | choice | date | datetime | bool | enc | enc_money
# 'enc' fields are encrypted: listed and filtered in Python, never grouped.
def _entities():
    from . import views as v
    return {
        'leads': {'view': v.LeadListView, 'date': 'received', 'fields': {
            'received': ('created_at', 'datetime'), 'name': ('name', 'enc'), 'phone': ('phone', 'enc'),
            'project': ('project__name', 'text'), 'source': ('source__name', 'text'),
            'status': ('status', 'choice'), 'tc_status': ('telecaller_status', 'choice'),
            'stm_status': ('stm_status', 'choice'), 'telecaller': ('telecaller__name', 'text'),
            'stm': ('stm__name', 'text'), 'channel_partner': ('channel_partner__name', 'enc'),
            'campaign': ('meta_campaign_name', 'text'), 'adset': ('meta_adset_name', 'enc'),
            'ad': ('meta_ad_name', 'enc'), 'city': ('city', 'text'), 'budget': ('budget_bucket', 'choice'),
            'duplicate': ('is_duplicate', 'bool'), 'stm_assigned': ('stm_assigned_at', 'datetime'),
        }},
        'site_visits': {'view': v.SiteVisitListView, 'date': 'visited', 'fields': {
            'scheduled': ('scheduled_at', 'datetime'), 'visited': ('visited_at', 'datetime'),
            'status': ('status', 'choice'), 'outcome': ('outcome', 'choice'),
            'project': ('project__name', 'text'), 'stm': ('stm__name', 'text'),
            'telecaller': ('referred_by_telecaller__name', 'text'),
            'client': ('lead__name', 'enc'), 'phone': ('lead__phone', 'enc'),
            'source': ('lead__source__name', 'text'), 'campaign': ('lead__meta_campaign_name', 'text'),
            'remarks': ('remarks', 'enc'),
        }},
        'follow_ups': {'view': v.FollowUpListView, 'date': 'scheduled', 'fields': {
            'scheduled': ('scheduled_at', 'datetime'), 'completed': ('completed_at', 'datetime'),
            'status': ('status', 'choice'), 'assigned_to': ('assigned_to__name', 'text'),
            'role': ('role_context', 'text'), 'project': ('lead__project__name', 'text'),
            'client': ('lead__name', 'enc'), 'phone': ('lead__phone', 'enc'),
            'remarks': ('remarks', 'enc'),
        }},
        'closures': {'view': v.ClosureListView, 'date': 'closure_date', 'fields': {
            'closure_date': ('closure_date', 'date'), 'status': ('status', 'choice'),
            'project': ('project__name', 'text'), 'stm': ('stm__name', 'text'),
            'telecaller': ('referred_by_telecaller__name', 'text'), 'unit': ('unit_no', 'text'),
            'unit_type': ('unit_type', 'text'), 'client': ('client_name', 'enc'),
            'total_amount': ('total_amount', 'enc_money'), 'booking_amount': ('booking_amount', 'enc_money'),
            'source': ('lead__source__name', 'text'),
        }},
        'bookings': {'view': v.BookingListCreateView, 'date': 'booking_date',
                     'params': {'mine': '1', 'scope': 'visible'}, 'fields': {
            'booking_date': ('booking_date', 'date'), 'status': ('status', 'choice'),
            'accounts_status': ('accounts_status', 'choice'), 'project': ('project__name', 'text'),
            'stm': ('stm__name', 'text'), 'source': ('source', 'text'), 'units': ('plot_numbers', 'text'),
            'client': ('client_name', 'enc'), 'amount': ('final_amount', 'enc_money'),
        }},
    }


TOOL = {
    'name': 'query_records',
    'description': (
        "Read the asker's own CRM records — only what their screens show them. Call it as many "
        "times as a question needs (e.g. this month and last month, then compare).\n"
        "Entities and fields:\n"
        "- leads: received, name*, phone*, project, source, status, tc_status, stm_status, telecaller, "
        "stm, channel_partner*, campaign, adset*, ad*, city, budget, duplicate, stm_assigned\n"
        "- site_visits: scheduled, visited, status (scheduled/completed/no_show/cancelled), outcome "
        "(hot/warm/cold/not_interested), project, stm, telecaller, client*, phone*, source, campaign, remarks*\n"
        "- follow_ups: scheduled, completed, status, assigned_to, role, project, client*, phone*, remarks*\n"
        "- closures: closure_date, status, project, stm, telecaller, unit, unit_type, client*, "
        "total_amount*, booking_amount*, source\n"
        "- bookings: booking_date, status (sold = approved by Sales/CP), accounts_status "
        "(pending/approved/rejected), project, stm, source, units, client*, amount*\n"
        "Fields marked * are encrypted: they can be listed, filtered with eq/contains and summed "
        "(amounts), but not grouped. Dates accept 'today', 'yesterday', 'this_week', 'this_month', "
        "'last_month', or YYYY-MM-DD. `book` picks partner-sourced (cp), non-partner (sales) or both "
        "(all, the default). Use mode 'count' or 'sum' with group_by for breakdowns, 'list' for rows."
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'entity': {'type': 'string', 'enum': ['leads', 'site_visits', 'follow_ups', 'closures', 'bookings']},
            'mode': {'type': 'string', 'enum': ['count', 'sum', 'list'],
                     'description': 'count rows, sum a money field, or list rows'},
            'filters': {'type': 'array', 'description': 'All must match.', 'items': {
                'type': 'object',
                'properties': {
                    'field': {'type': 'string'},
                    'op': {'type': 'string', 'enum': ['eq', 'ne', 'in', 'contains', 'gte', 'lte', 'isnull']},
                    'value': {'description': 'string, number, boolean, or a list for "in"'},
                },
                'required': ['field', 'op'],
            }},
            'group_by': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 2,
                         'description': 'Up to two non-encrypted fields. Dates group by day; use '
                                        '"<date field>:month" or ":week" for coarser buckets.'},
            'sum_field': {'type': 'string', 'description': 'The money field for mode "sum".'},
            'fields': {'type': 'array', 'items': {'type': 'string'},
                       'description': 'Columns for mode "list" (default: a sensible few).'},
            'order_by': {'type': 'string', 'description': 'A field, "-field" for newest/largest first.'},
            'limit': {'type': 'integer', 'description': f'Rows for mode "list" (max {LIST_LIMIT}).'},
            'book': {'type': 'string', 'enum': ['sales', 'cp', 'all']},
        },
        'required': ['entity', 'mode'],
    },
}

SYSTEM = (
    "You are Ask Nexora, the analytics assistant inside Nexora, the CRM of a real-estate developer "
    "in Gujarat, India. You answer the signed-in person's questions about leads, site visits (SV), "
    "follow-ups, closures and bookings, using the query_records tool — never invent numbers.\n\n"
    "How the business works: leads arrive (Meta ads, walk-ins, channel partners, references); a "
    "telecaller (TC) calls them and passes interested ones to an STM (sales executive), who books a "
    "site visit; a visit ends Hot/Warm/Cold/Not Interested; a deal becomes a closure and a booking, "
    "which Sales/CP approve (status sold) and then Accounts signs off (accounts_status approved). "
    "Projects include Pratishtha, Kalrav, Anahata Florenza, Tundav and others.\n\n"
    "Answering:\n"
    "- Query before you answer; for comparisons or 'why' questions run several queries (periods, "
    "projects, people, funnel steps) and compute rates yourself.\n"
    "- Lead with the answer and the key numbers, then what they mean, then 2-3 practical next steps "
    "when the question asks for analysis. Keep it short; use a small Markdown table when comparing.\n"
    "- Money is in Indian rupees: write ₹ with Indian grouping (₹1,23,45,678) or Lakh/Crore (₹4.2 L, ₹3.1 Cr).\n"
    "- Only list client names or phone numbers when the person asks for a list of records.\n"
    "- The data is already limited to what this person may see; if a number looks small, say it "
    "covers their scope. If data looks inconsistent (missing dates, future dates), point it out.\n"
    "- If a question is outside this data (AR, HR, accounts ledgers), say so plainly."
)


# ── Running a query ──────────────────────────────────────────────────────────
def _scoped_qs(request_user, entity, book, company_id):
    """The entity's queryset exactly as the asker's list screen would scope it."""
    from rest_framework.test import APIRequestFactory, force_authenticate
    spec = _entities()[entity]
    params = dict(spec.get('params', {}))
    params['book'] = book or 'all'
    if company_id:
        params['company_id'] = str(company_id)
    req = APIRequestFactory().get('/ai/', params)
    force_authenticate(req, user=request_user)
    req._ai_capture = {}
    spec['view'].as_view()(req)
    qs = req._ai_capture.get('qs')
    if qs is None:
        raise ValueError('That list is not available to you.')
    return qs.order_by()


def _range(value):
    """A date keyword or YYYY-MM-DD → (start, end) local dates, inclusive."""
    today = timezone.localdate()
    v = str(value or '').strip().lower()
    if v == 'today':
        return today, today
    if v == 'yesterday':
        y = today - timedelta(days=1)
        return y, y
    if v == 'this_week':
        return today - timedelta(days=today.weekday()), today
    if v == 'this_month':
        return today.replace(day=1), today
    if v == 'last_month':
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    d = parse_date(v[:10])
    if d is None:
        raise ValueError(f'Not a date: {value!r}')
    return d, d


def _apply_filters(qs, fields, filters):
    """DB filters for plain fields; returns (qs, python_checks) for encrypted ones."""
    from django.db.models import Q
    checks = []
    for f in filters or []:
        alias, op, value = f.get('field'), f.get('op'), f.get('value')
        if alias not in fields:
            raise ValueError(f'Unknown field {alias!r}.')
        path, kind = fields[alias]
        if kind in ('enc', 'enc_money'):
            if op not in ('eq', 'contains', 'isnull', 'gte', 'lte'):
                raise ValueError(f'{alias} is encrypted: use eq, contains, gte or lte.')
            checks.append((path, op, value))
            continue
        if kind in ('date', 'datetime') and op in ('eq', 'gte', 'lte'):
            start, end = _range(value)
            lk = f'{path}__date' if kind == 'datetime' else path
            if op == 'eq':
                qs = qs.filter(**{f'{lk}__gte': start, f'{lk}__lte': end})
            elif op == 'gte':
                qs = qs.filter(**{f'{lk}__gte': start})
            else:
                qs = qs.filter(**{f'{lk}__lte': end})
            continue
        if op == 'isnull':
            qs = qs.filter(**{f'{path}__isnull': bool(value)}) if kind != 'text' else (
                qs.filter(Q(**{f'{path}__isnull': True}) | Q(**{path: ''})) if value
                else qs.exclude(**{f'{path}__isnull': True}).exclude(**{path: ''}))
        elif op == 'eq':
            qs = qs.filter(**{f'{path}__iexact' if kind == 'text' else path: value})
        elif op == 'ne':
            qs = qs.exclude(**{f'{path}__iexact' if kind == 'text' else path: value})
        elif op == 'in':
            vals = value if isinstance(value, list) else [value]
            if kind == 'text':
                q = Q()
                for x in vals:
                    q |= Q(**{f'{path}__iexact': x})
                qs = qs.filter(q)
            else:
                qs = qs.filter(**{f'{path}__in': vals})
        elif op == 'contains':
            qs = qs.filter(**{f'{path}__icontains': value})
        elif op in ('gte', 'lte'):
            qs = qs.filter(**{f'{path}__{op}': value})
    return qs, checks


def _passes(row, checks):
    for path, op, value in checks:
        got = row.get(path)
        if op == 'isnull':
            if bool(value) != (got in (None, '')):
                return False
            continue
        if op in ('gte', 'lte'):
            try:
                a, b = Decimal(str(got or 0)), Decimal(str(value))
            except Exception:
                return False
            if (op == 'gte' and a < b) or (op == 'lte' and a > b):
                return False
            continue
        g, v = str(got or '').lower(), str(value or '').lower()
        if (op == 'eq' and g != v) or (op == 'contains' and v not in g):
            return False
    return True


def _fmt(value, kind):
    if value is None:
        return ''
    if kind == 'datetime':
        return timezone.localtime(value).strftime('%Y-%m-%d %H:%M')
    if kind == 'date':
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def run_query(user, company_id, args):
    """Execute one query_records call; returns a JSON-able dict for the model."""
    from django.db.models import Count, F
    from django.db.models.functions import TruncDate, TruncMonth, TruncWeek
    ents = _entities()
    entity = args.get('entity')
    if entity not in ents:
        raise ValueError(f'Unknown entity {entity!r}.')
    fields = ents[entity]['fields']
    mode = args.get('mode') or 'count'
    qs = _scoped_qs(user, entity, args.get('book'), company_id)
    qs, checks = _apply_filters(qs, fields, args.get('filters'))

    if mode == 'list':
        cols = [c for c in (args.get('fields') or []) if c in fields] or list(fields)[:6]
        limit = max(1, min(LIST_LIMIT, int(args.get('limit') or 25)))
        order = args.get('order_by') or ('-' + ents[entity]['date'])
        desc = order.startswith('-')
        oalias = order.lstrip('-')
        if oalias in fields and fields[oalias][1] not in ('enc', 'enc_money'):
            qs = qs.order_by(('-' if desc else '') + fields[oalias][0])
        paths = [fields[c][0] for c in cols] + [p for p, _, _ in checks]
        rows = []
        for r in qs.values(*dict.fromkeys(paths))[:SCAN_LIMIT if checks else limit]:
            if checks and not _passes(r, checks):
                continue
            rows.append({c: _fmt(r.get(fields[c][0]), fields[c][1]) for c in cols})
            if len(rows) >= limit:
                break
        return {'rows': rows, 'shown': len(rows),
                'total_matching': None if checks else qs.count()}

    groups = args.get('group_by') or []
    gexpr = {}
    for g in groups:
        alias, _, unit = g.partition(':')
        if alias not in fields or fields[alias][1] in ('enc', 'enc_money'):
            raise ValueError(f'Cannot group by {alias!r}.')
        path, kind = fields[alias]
        if kind in ('date', 'datetime'):
            fn = {'month': TruncMonth, 'week': TruncWeek}.get(unit, TruncDate)
            gexpr[g] = fn(path)
        else:
            gexpr[g] = F(path)

    if mode == 'sum':
        sf = args.get('sum_field')
        if sf not in fields or fields[sf][1] != 'enc_money':
            raise ValueError('sum_field must be a money field (total_amount, booking_amount, amount).')
        money = fields[sf][0]
        need = dict.fromkeys([money] + [p for p, _, _ in checks])
        totals, n = {}, 0
        ann = {f'_g{i}': e for i, e in enumerate(gexpr.values())}
        for r in qs.annotate(**ann).values(*need, *ann)[:SCAN_LIMIT]:
            if checks and not _passes(r, checks):
                continue
            key = tuple(_fmt(r[f'_g{i}'], 'date') if hasattr(r[f'_g{i}'], 'isoformat') else r[f'_g{i}']
                        for i in range(len(ann)))
            totals[key] = totals.get(key, Decimal('0')) + Decimal(str(r.get(money) or 0))
            n += 1
        out = [dict(zip(groups, k), total=float(v)) for k, v in
               sorted(totals.items(), key=lambda kv: -kv[1])]
        return {'groups': out[:200], 'rows_summed': n, 'grand_total': float(sum(totals.values()))}

    # count
    if checks:
        need = dict.fromkeys([p for p, _, _ in checks])
        ann = {f'_g{i}': e for i, e in enumerate(gexpr.values())}
        counts = {}
        for r in qs.annotate(**ann).values(*need, *ann)[:SCAN_LIMIT]:
            if _passes(r, checks):
                key = tuple(r[f'_g{i}'] for i in range(len(ann)))
                counts[key] = counts.get(key, 0) + 1
        out = [dict(zip(groups, [_fmt(x, 'date') if hasattr(x, 'isoformat') else x for x in k]), count=v)
               for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
        return {'groups': out[:200] if groups else None, 'total': sum(counts.values())}
    if not groups:
        return {'total': qs.count()}
    ann = {f'_g{i}': e for i, e in enumerate(gexpr.values())}
    rows = (qs.annotate(**ann).values(*ann).annotate(count=Count('id')).order_by('-count')[:200])
    out = []
    for r in rows:
        d = {}
        for i, g in enumerate(groups):
            x = r[f'_g{i}']
            d[g] = x.isoformat() if hasattr(x, 'isoformat') else x
        d['count'] = r['count']
        out.append(d)
    return {'groups': out, 'total': qs.count()}


# ── The conversation ─────────────────────────────────────────────────────────
def _client():
    import anthropic
    return anthropic.Anthropic(max_retries=2, timeout=120.0)


def _cost(usage):
    """Approximate ₹ for one question from summed token usage."""
    usd = (usage['input'] * PRICE_IN + usage['output'] * PRICE_OUT
           + usage['cache_read'] * PRICE_CACHE_READ + usage['cache_write'] * PRICE_CACHE_WRITE) / 1e6
    return round(usd * USD_INR, 2)


def answer(user, company_id, question, history, module='sales'):
    """Run the tool loop for one question. Returns (text, usage, queries)."""
    client = _client()
    who = f"{user.name or 'User'} ({user.role or ''}{', ' + user.designation if user.designation else ''})"
    today = timezone.localdate()
    context = (f"[Today is {today:%A, %d %B %Y} (India). Asked by {who}. "
               f"Data is limited to what this person can see."
               + (" They are in the Channel Partner module: use book 'cp' unless they ask about Sales."
                  if module == 'cp' else '') + "]\n\n")
    messages = []
    for turn in (history or [])[-6:]:            # earlier questions and answers, as text
        q, a = str(turn.get('q') or '')[:2000], str(turn.get('a') or '')[:4000]
        if q and a:
            messages += [{'role': 'user', 'content': q}, {'role': 'assistant', 'content': a}]
    messages.append({'role': 'user', 'content': context + question})
    usage = {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}
    queries = 0
    for _ in range(MAX_ROUNDS):
        resp = client.messages.create(
            model=MODEL, max_tokens=16000, system=SYSTEM, tools=[TOOL], messages=messages,
            # Reuse the fixed instructions + tool between questions (cheaper input), effort
            # medium, and route a refused request to another model instead of stopping.
            extra_headers={'anthropic-beta': 'server-side-fallback-2026-07-01'},
            extra_body={'cache_control': {'type': 'ephemeral'}, 'output_config': {'effort': EFFORT},
                        'fallbacks': 'default'},
        )
        u = resp.usage
        usage['input'] += getattr(u, 'input_tokens', 0) or 0
        usage['output'] += getattr(u, 'output_tokens', 0) or 0
        usage['cache_read'] += getattr(u, 'cache_read_input_tokens', 0) or 0
        usage['cache_write'] += getattr(u, 'cache_creation_input_tokens', 0) or 0
        if resp.stop_reason == 'refusal':
            return "I can't help with that one — try asking about leads, visits, follow-ups or bookings.", usage, queries
        # The assistant turn goes back exactly as it came (append-only).
        content = resp.to_dict()['content']
        messages.append({'role': 'assistant', 'content': content})
        calls = [b for b in content if b.get('type') == 'tool_use']
        if resp.stop_reason != 'tool_use' or not calls:
            text = '\n'.join(b.get('text', '') for b in content if b.get('type') == 'text').strip()
            if resp.stop_reason == 'max_tokens':
                text += '\n\n_(The answer was cut short — ask a narrower question.)_'
            return text or 'I could not work that out — try rephrasing.', usage, queries
        results = []
        for b in calls:
            queries += 1
            try:
                out = run_query(user, company_id, b.get('input') or {})
                results.append({'type': 'tool_result', 'tool_use_id': b['id'],
                                'content': json.dumps(out, default=str)[:60000]})
            except Exception as e:                      # tell the model, let it adjust
                results.append({'type': 'tool_result', 'tool_use_id': b['id'],
                                'content': f'Error: {e}', 'is_error': True})
        messages.append({'role': 'user', 'content': results})
    return 'That needed more steps than allowed — try a narrower question.', usage, queries


# ── Jobs ─────────────────────────────────────────────────────────────────────
def _path(job):
    return os.path.join(JOB_DIR, ''.join(c for c in str(job) if c.isalnum()) + '.json')


def _write(job, data):
    os.makedirs(JOB_DIR, exist_ok=True)
    tmp = _path(job) + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, _path(job))


def _read(job):
    try:
        with open(_path(job)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _log(user, question, state, usage, queries, request_path):
    try:
        from activity.models import ActivityLog
        rupees = _cost(usage)
        ActivityLog.objects.create(
            company_id=getattr(user, 'company_id', None), actor=user, actor_name=user.name or '',
            module='Sales', action='asked', target_type='ai question', target_id='',
            summary=('Asked Nexora AI — %s' % question)[:500],
            details=json.dumps({'question': question[:1000], 'status': state, 'queries': queries,
                                'tokens': usage, 'approx_cost_inr': rupees, 'model': MODEL}),
            method='POST', path=request_path, status_code=200)
    except Exception:
        pass


class AskView(APIView):
    """POST {question, history?, company_id?} → {job}; the answer arrives via AskJobView."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if not can_use_ai(user):
            return Response({'detail': 'Ask Nexora is not switched on for you. Ask an admin to tick it in User Management.'},
                            status=status.HTTP_403_FORBIDDEN)
        if not os.getenv('ANTHROPIC_API_KEY'):
            return Response({'detail': 'Ask Nexora is not set up yet — the AI key is missing on the server.'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        question = str(request.data.get('question') or '').strip()[:1500]
        if not question:
            return Response({'question': ['Type a question.']}, status=status.HTTP_400_BAD_REQUEST)
        key = f'ai_q_{user.id}_{timezone.localdate():%Y%m%d}'
        used = cache.get(key, 0)
        if used >= DAILY_LIMIT and not getattr(settings, 'AI_NO_LIMIT', False):
            return Response({'detail': f'You have used today\'s {DAILY_LIMIT} questions. Try again tomorrow.'},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        cache.set(key, used + 1, 60 * 60 * 26)
        history = request.data.get('history') if isinstance(request.data.get('history'), list) else []
        company_id = request.data.get('company_id')
        module = 'cp' if request.data.get('module') == 'cp' else 'sales'
        from .views import is_platform_admin
        if not is_platform_admin(user):
            company_id = None                    # only a platform admin picks a company
        job = uuid.uuid4().hex
        _write(job, {'owner': user.id, 'status': 'running'})
        path = request.path

        def run():
            try:
                text, usage, queries = answer(user, company_id, question, history, module)
                _write(job, {'owner': user.id, 'status': 'done', 'answer': text,
                             'approx_cost_inr': _cost(usage)})
                _log(user, question, 'answered', usage, queries, path)
            except Exception as e:
                import anthropic
                msg = 'Ask Nexora could not answer right now. Try again in a minute.'
                if isinstance(e, anthropic.AuthenticationError):
                    msg = 'Ask Nexora is not set up correctly — the AI key was refused.'
                elif isinstance(e, anthropic.RateLimitError):
                    msg = 'Ask Nexora is busy right now. Try again in a minute.'
                elif isinstance(e, anthropic.BadRequestError) and 'credit' in str(e).lower():
                    msg = 'Ask Nexora has run out of AI credit. Ask an admin to top it up.'
                _write(job, {'owner': user.id, 'status': 'error', 'detail': msg})
                _log(user, question, 'failed: %s' % type(e).__name__,
                     {'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0}, 0, path)
            finally:
                if not getattr(settings, 'AI_INLINE', False):
                    connection.close()

        if getattr(settings, 'AI_INLINE', False):
            run()
        else:
            threading.Thread(target=run, daemon=True).start()
        return Response({'job': job}, status=status.HTTP_202_ACCEPTED)


class AskJobView(APIView):
    """GET → {status: running|done|error, answer?, detail?} for the asker's own job."""
    permission_classes = [IsAuthenticated]

    def get(self, request, job):
        st = _read(job)
        if not st or st.get('owner') != request.user.id:
            return Response({'detail': 'That question has expired — ask it again.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({k: st.get(k) for k in ('status', 'answer', 'detail')})
