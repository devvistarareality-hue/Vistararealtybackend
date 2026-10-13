"""Ask Nexora's dashboard_numbers tool: the Sales dashboard's own figures (and the
ratios it shows) for a period, a person and a project — so "called", "MQL→SV ratio",
"SQL" and the rest are exactly what the dashboard tile shows, not a re-derivation.

For a named person the dashboard is computed AS that person (what "View as" shows),
which is only allowed for someone the asker may see: themselves, their reporting
tree, or anyone for an admin / company-wide manager.
"""
from rest_framework.test import APIRequestFactory, force_authenticate

TOOL = {
    'name': 'dashboard_numbers',
    'description': (
        "The Sales (or Channel Partner) dashboard's figures for a period — exactly the tiles: leads, "
        "called (MQL), warm/SQL, site visits done, upcoming visits, follow-ups pending/overdue, "
        "callbacks, closures, and the ratios MQL→SV, SQL→SV, SQL→closure. Use it for any dashboard-"
        "style number or ratio, especially for one person ('Varsha's called to SV ratio in September')."
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'period': {'type': 'string', 'description': "today, yesterday, this_week, this_month, last_month, "
                                                        "this_quarter, last_quarter, this_fy, last_fy, or omit"},
            'date_from': {'type': 'string', 'description': 'YYYY-MM-DD (instead of period)'},
            'date_to': {'type': 'string', 'description': 'YYYY-MM-DD'},
            'person': {'type': 'string', 'description': 'A telecaller / STM name; omit for the asker\'s own view'},
            'project': {'type': 'string', 'description': 'A project name; omit for all'},
            'book': {'type': 'string', 'enum': ['sales', 'cp', 'all']},
        },
    },
}

LABELS = {
    'total_leads': 'Leads', 'new_leads': 'New (not yet worked)', 'called_count': 'Called (MQL)',
    'to_call_count': 'To call', 'total_called_count': 'Total calls (incl. follow-up calls)',
    'followup_call_count': 'Follow-up calls', 'warm_count': 'Warm (TC)', 'sql_count': 'SQL',
    'sv_done': 'Site visits done', 'stm_sv_scheduled_count': 'Upcoming site visits',
    'callback_count': 'Callbacks', 'followup_pending_count': 'Follow-ups pending',
    'followup_overdue_count': 'Follow-ups overdue', 'closures': 'Closures (recorded)',
    'not_reachable_count': 'Not reachable', 'cold_count': 'Cold', 'stm_hot_count': 'STM hot',
    'stm_warm_count': 'STM warm', 'stm_cold_count': 'STM cold', 'accounts_pending': 'Awaiting Accounts',
}


def _ratio(a, b):
    return f'{a / b:.1f} : 1' if b else '—'


def run(user, company_id, args):
    from accounts.models import User
    from . import assistant
    from .views import StatsView, _sees_all_company, _visible_user_ids, is_platform_admin
    params = {}
    if args.get('period'):
        a, z = assistant._range(args['period'])
        params.update(date_from=a.isoformat(), date_to=z.isoformat())
    if args.get('date_from'):
        params['date_from'] = args['date_from']
    if args.get('date_to'):
        params['date_to'] = args['date_to']
    params['book'] = assistant._book_for(user, args.get('book') or 'sales')
    if params['book'] == 'cp':
        params['cp_only'] = 'true'
    cid = company_id if (company_id and is_platform_admin(user)) else user.company_id
    as_user = user
    if args.get('person'):
        name = str(args['person']).strip()
        cands = User.objects.filter(company_id=cid, is_active=True, name__icontains=name)
        exact = [u for u in cands if (u.name or '').lower() == name.lower()]
        person = exact[0] if exact else (cands.first() if cands.count() == 1 else None)
        if not person:
            names = ', '.join(u.name for u in cands[:6]) or 'nobody'
            raise ValueError(f'No single person matches {name!r} (found: {names}). Ask which one.')
        if person.id != user.id and not _sees_all_company(user) and person.id not in _visible_user_ids(user):
            raise ValueError(f'{person.name} is outside what you can see.')
        as_user = person
    if args.get('project'):
        from .models import Project
        p = Project.objects.filter(company_id=cid, name__iexact=str(args['project']).strip()).first() \
            or Project.objects.filter(company_id=cid, name__icontains=str(args['project']).strip()).first()
        if not p:
            raise ValueError(f'No project called {args["project"]!r}.')
        params['project_id'] = str(p.id)
    if as_user is user and is_platform_admin(user):
        params['company_id'] = str(cid)
    req = APIRequestFactory().get('/ai/', params)
    force_authenticate(req, user=as_user)
    data = StatsView.as_view()(req).data
    out = {LABELS[k]: data[k] for k in LABELS if isinstance(data.get(k), (int, float))}
    called, sv, sql, cl = (data.get('called_count') or 0), (data.get('sv_done') or 0), \
        (data.get('sql_count') or data.get('warm_count') or 0), (data.get('closures') or 0)
    out['MQL→SV ratio'] = _ratio(called, sv)
    out['SQL→SV ratio'] = _ratio(sql, sv)
    out['SQL→Closure ratio'] = _ratio(sql, cl)
    return {'for': as_user.name, 'period': [params.get('date_from'), params.get('date_to')],
            'project': args.get('project') or 'all', 'book': params['book'], 'figures': out}
