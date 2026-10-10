"""What the assistant is allowed to look up, and how.

The model never sees a row of the database. It sees a question and the list of
tools below; it picks one and names the arguments; this module runs the query and
hands back counts. That ordering is the whole design:

  * Numbers cannot be hallucinated — they come from the ORM, not the model.
  * Nothing it was not given a tool for can be asked about, which is the right
    failure mode for a system holding other people's client lists.
  * No personal data leaves the building. Every tool returns aggregates —
    counts, and project names. Never a client name, phone number or address.

SCOPING IS ENFORCED HERE, NOT IN THE PROMPT. Every queryset below goes through
`scope_to_company`, and the lead-derived ones additionally through
`scope_leads_to_role`, using the *asking* user. A prompt that says "only show
this company" is a suggestion; a filter is a control. Get this wrong and one
company reads another's pipeline, which is the kind of leak nobody notices until
a customer does.
"""
from datetime import date, datetime

from django.db.models import Count

from accounts.permissions import scope_to_company
from sales.models import Booking, Lead, Project, SiteVisit

# Advertised to the model. Keep the descriptions plain: they are the only thing
# telling it when each tool applies.
TOOL_SPECS = [
    {
        'name': 'site_visit_stats',
        'description': (
            'Counts of site visits over a date range, broken down by status and by '
            'project. Visits are dated by when they were scheduled. Use for questions '
            'about site visits, viewings, or who visited which project.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'date_from': {'type': 'string', 'description': 'Start date, YYYY-MM-DD. Inclusive.'},
                'date_to': {'type': 'string', 'description': 'End date, YYYY-MM-DD. Inclusive.'},
                'project': {'type': 'string', 'description': 'Optional project name to narrow to.'},
            },
            'required': ['date_from', 'date_to'],
        },
    },
    {
        'name': 'booking_stats',
        'description': (
            'Counts and total value of bookings over a date range, broken down by '
            'stage (sold, pending approval, rejected, draft) and by project. Use for '
            'questions about bookings, sales, deals or revenue.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'date_from': {'type': 'string', 'description': 'Start date, YYYY-MM-DD. Inclusive.'},
                'date_to': {'type': 'string', 'description': 'End date, YYYY-MM-DD. Inclusive.'},
                'project': {'type': 'string', 'description': 'Optional project name to narrow to.'},
            },
            'required': ['date_from', 'date_to'],
        },
    },
    {
        'name': 'lead_stats',
        'description': (
            'Counts of leads created over a date range, broken down by status, by '
            'source and by project. Use for questions about leads, enquiries or the '
            'top of the pipeline.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'date_from': {'type': 'string', 'description': 'Start date, YYYY-MM-DD. Inclusive.'},
                'date_to': {'type': 'string', 'description': 'End date, YYYY-MM-DD. Inclusive.'},
                'project': {'type': 'string', 'description': 'Optional project name to narrow to.'},
            },
            'required': ['date_from', 'date_to'],
        },
    },
]

TOOL_NAMES = {t['name'] for t in TOOL_SPECS}


def _day(value):
    """A YYYY-MM-DD string as a date, or None. The model supplies these, so they
    are treated as untrusted input rather than assumed well-formed."""
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _project_filter(qs, user, name, path='project'):
    """Narrow to a project by name, matched within the user's own company.

    Matched by name because that is what someone types; resolved against the
    scoped project list so a name that exists in two companies cannot be used to
    reach across into the other one.
    """
    if not name:
        return qs, None
    hit = (scope_to_company(Project.objects.all(), user)
           .filter(name__icontains=str(name).strip()).first())
    if not hit:
        return qs.none(), None
    return qs.filter(**{f'{path}_id': hit.id}), hit.name


def _counts(qs, field):
    """`field` -> count, dropping empties. Small dicts: this is what gets sent."""
    out = {}
    for row in qs.values(field).annotate(n=Count('id')).order_by('-n'):
        key = row[field]
        if key:
            out[str(key)] = row['n']
    return out


def _sum_money(qs, field):
    """Total an encrypted money column.

    final_amount is an EncryptedDecimalField, so the database holds ciphertext —
    SUM() over it does not fail, it quietly returns 0, which is how a wrong total
    reaches a screen looking perfectly reasonable. The values have to come back
    through the field's decryption and be added in Python.

    Only the ids and that one column are fetched, so this stays cheap on the
    few-hundred-row ranges these questions ask about.
    """
    return int(sum(v or 0 for v in qs.values_list(field, flat=True)))


def site_visit_stats(user, date_from, date_to, project=None):
    start, end = _day(date_from), _day(date_to)
    if not start or not end:
        return {'error': 'Dates must be YYYY-MM-DD.'}
    qs = scope_to_company(SiteVisit.objects.all(), user, 'lead__company')
    from sales.views import scope_leads_to_role
    qs = scope_leads_to_role(qs, user, 'lead__')
    qs = qs.filter(scheduled_at__date__gte=start, scheduled_at__date__lte=end)
    qs, proj = _project_filter(qs, user, project)
    return {
        'range': f'{start} to {end}',
        'project': proj or 'all projects',
        'total': qs.count(),
        'by_status': _counts(qs, 'status'),
        'by_project': _counts(qs, 'project__name'),
    }


def booking_stats(user, date_from, date_to, project=None):
    start, end = _day(date_from), _day(date_to)
    if not start or not end:
        return {'error': 'Dates must be YYYY-MM-DD.'}
    qs = scope_to_company(Booking.objects.all(), user)
    qs = qs.filter(booking_date__gte=start, booking_date__lte=end)
    qs, proj = _project_filter(qs, user, project)
    sold = qs.filter(status='sold')
    return {
        'range': f'{start} to {end}',
        'project': proj or 'all projects',
        'total': qs.count(),
        'by_stage': _counts(qs, 'status'),
        'by_project': _counts(qs, 'project__name'),
        'sold_count': sold.count(),
        # Only the sold ones are totalled: adding draft and rejected money into one
        # figure would read as revenue and would not be.
        'sold_value': _sum_money(sold, 'final_amount'),
    }


def lead_stats(user, date_from, date_to, project=None):
    start, end = _day(date_from), _day(date_to)
    if not start or not end:
        return {'error': 'Dates must be YYYY-MM-DD.'}
    qs = scope_to_company(Lead.objects.all(), user)
    from sales.views import scope_leads_to_role
    qs = scope_leads_to_role(qs, user)
    qs = qs.filter(created_at__date__gte=start, created_at__date__lte=end)
    qs, proj = _project_filter(qs, user, project)
    return {
        'range': f'{start} to {end}',
        'project': proj or 'all projects',
        'total': qs.count(),
        'by_status': _counts(qs, 'status'),
        'by_source': _counts(qs, 'source__name'),
        'by_project': _counts(qs, 'project__name'),
    }


_RUNNERS = {
    'site_visit_stats': site_visit_stats,
    'booking_stats': booking_stats,
    'lead_stats': lead_stats,
}


def run_tool(name, args, user):
    """Run one tool for `user`. Unknown names and bad arguments come back as data,
    not exceptions — the model is told what went wrong and can correct itself."""
    fn = _RUNNERS.get(name)
    if not fn:
        return {'error': f'No such tool: {name}'}
    if not isinstance(args, dict):
        return {'error': 'Arguments must be an object.'}
    allowed = {'date_from', 'date_to', 'project'}
    clean = {k: v for k, v in args.items() if k in allowed}
    try:
        return fn(user, **clean)
    except TypeError as exc:
        return {'error': f'Wrong arguments: {exc}'}
