"""Which notifications a person gets — set per designation (per company), with an
optional per-person override.

Each notification `type` sent by notify() belongs to a *kind* — the thing an admin
switches on or off in Designation Master → Permissions → Notifications. Several types
share a kind (a booking approved / rejected / cancelled are one switch), and the two
"Team … overdue" escalations have kinds of their own, because a manager hearing about
their team is a different choice from a rep hearing about their own work.

A kind's setting:
    mode    'push'  — bell + phone push (today's behaviour, the default)
            'inapp' — bell only, no push
            'off'   — not sent at all
    levels  team kinds only: how far up the reporting chain the alert goes
            (1 = the direct manager only; 5 = everyone up to five levels, as before)
    digest  team kinds only: no alert per item — one morning summary instead

Nothing is configured → every kind is 'push' with levels 5 and no digest, i.e. exactly
what happened before this existed, so nobody silently stops getting alerts.
"""

DEFAULT = {'mode': 'push', 'levels': 5, 'digest': False}
MODES = ('push', 'inapp', 'off')
MAX_LEVELS = 5

# key, label, group, what it is, team kind?, module — the module whose menu it belongs to
# (the editor shows a designation only the modules its menu answers for; Channel
# Partner shares the Sales notifications).
KINDS = [
    ('new_lead', 'New lead assigned', 'Leads', 'A lead (or a batch) is handed to them.', False, 'Sales'),
    ('re_enquiry', 'Lead enquired again', 'Leads', 'One of their leads came in again.', False, 'Sales'),
    ('lead_transfer', 'Lead transfers', 'Leads', 'A transfer is asked of them, or theirs is approved / rejected.', False, 'Sales'),
    ('followup', 'New follow-up', 'Follow-ups', 'A follow-up is booked for them.', False, 'Sales'),
    ('followup_overdue', 'My follow-up overdue', 'Follow-ups', 'Their own follow-up has passed its time.', False, 'Sales'),
    ('team_followup_overdue', 'Team follow-up overdue', 'Follow-ups', 'Someone under them missed a follow-up by a day.', True, 'Sales'),
    ('sv', 'Site visit scheduled', 'Site visits', 'A site visit is booked on their lead.', False, 'Sales'),
    ('sv_done', 'Site visit done', 'Site visits', 'A site visit on their lead is completed.', False, 'Sales'),
    ('sv_overdue', 'My site visit overdue', 'Site visits', 'Their own visit was not marked done in time.', False, 'Sales'),
    ('team_sv_overdue', 'Team site visit overdue', 'Site visits', 'A visit under them is a day past and not done.', True, 'Sales'),
    ('booking_submitted', 'Booking submitted', 'Bookings', 'A booking they made is waiting for approval.', False, 'Sales'),
    ('booking_approval', 'Booking needs my approval', 'Bookings', 'A booking (or its revision) on a project they approve.', False, 'Sales'),
    ('booking_result', 'Booking approved / rejected / cancelled', 'Bookings', 'The outcome of a booking they made, and new closures.', False, 'Sales'),
    ('accounts_booking_approval', 'Accounts approval needed', 'Accounts', 'A booking waits for their Accounts sign-off.', False, 'Accounts & Finance'),
    ('accounts_booking_result', 'Accounts approved / rejected / cancelled', 'Accounts', 'The Accounts outcome of a booking.', False, 'Accounts & Finance'),
    ('ar_followup', 'Collection follow-ups', 'AR', 'A collection follow-up is assigned to them or falls due.', False, 'AR'),
    ('ar_followup_overdue', 'Collection follow-up overdue', 'AR', 'A collection follow-up passed its time.', False, 'AR'),
    ('ar_collections_digest', "Today's collections", 'AR', 'The morning summary of overdue and due-soon accounts.', False, 'AR'),
    ('ar_due_soon', 'Payment due soon', 'AR', 'A customer instalment falls due in a few days.', False, 'AR'),
    ('task', 'Tasks', 'Tasks', 'A task is assigned, commented on or nearly due.', False, 'Task Allocation'),
    ('investor', 'Club 1000 investors', 'Club 1000', 'An investor waits for approval, or theirs is decided.', False, 'Club 1000'),
    ('project_pending_approval', 'New project needs approval', 'Other', 'A new project waits for their approval.', False, 'Sales'),
    ('availability_reminder', 'Mark yourself available', 'Other', 'The morning reminder to sign in for leads.', False, 'Sales'),
]
KIND_KEYS = [k[0] for k in KINDS]
TEAM_KINDS = {k[0] for k in KINDS if k[4]}
KIND_MODULE = {k[0]: k[5] for k in KINDS}

# notify() type → kind, where they differ. Anything not listed is its own kind.
TYPE_KIND = {
    'lead_transfer_requested': 'lead_transfer', 'lead_transfer_approved': 'lead_transfer',
    'lead_transfer_rejected': 'lead_transfer',
    'booking_update': 'booking_approval',
    'booking_approved': 'booking_result', 'booking_rejected': 'booking_result',
    'booking_cancelled': 'booking_result', 'closure': 'booking_result',
    'accounts_booking_update': 'accounts_booking_approval',
    'accounts_booking_approved': 'accounts_booking_result', 'accounts_booking_rejected': 'accounts_booking_result',
    'accounts_booking_cancelled': 'accounts_booking_result',
    'ar_followup_assigned': 'ar_followup', 'ar_followup_due': 'ar_followup',
    'task_assigned': 'task', 'task_comment': 'task', 'task_due_soon': 'task',
    'investor_approval': 'investor', 'investor_approved': 'investor', 'investor_rejected': 'investor',
    'team_digest': 'team_followup_overdue',
}

# One-click starting points in the editor (an admin can change any row afterwards).
PRESETS = {
    'everything': {'label': 'Everything (as before)', 'prefs': {}},
    'normal': {'label': 'Normal — team alerts to direct manager',
               'prefs': {k: {'mode': 'push', 'levels': 1, 'digest': False} for k in TEAM_KINDS}},
    'quiet': {'label': 'Quiet — team alerts as a morning summary',
              'prefs': {k: {'mode': 'push', 'levels': 1, 'digest': True} for k in TEAM_KINDS}},
}


def kind_of(ntype):
    return TYPE_KIND.get(ntype, ntype)


def clean(value):
    """Validate prefs from the editor: known kinds only, known modes, levels 1-5."""
    out = {}
    if not isinstance(value, dict):
        return out
    for k, v in value.items():
        if k not in KIND_KEYS or not isinstance(v, dict):
            continue
        mode = v.get('mode') if v.get('mode') in MODES else 'push'
        row = {'mode': mode}
        if k in TEAM_KINDS:
            try:
                row['levels'] = max(1, min(MAX_LEVELS, int(v.get('levels') or MAX_LEVELS)))
            except (TypeError, ValueError):
                row['levels'] = MAX_LEVELS
            row['digest'] = bool(v.get('digest'))
        out[k] = row
    return out


def setting(user, kind):
    """The effective setting of one kind for one person: designation, then their own
    override, over the default. Never raises — a lookup problem means "as before"."""
    out = dict(DEFAULT)
    try:
        from .capabilities import _designation_row
        cache = getattr(user, '_notif_prefs_cache', None)
        if cache is None:
            row = _designation_row(user)
            cache = (getattr(row, 'notification_prefs', None) or {}) if row is not None else {}
            try:
                user._notif_prefs_cache = cache
            except Exception:
                pass
        out.update(cache.get(kind) or {})
        out.update((getattr(user, 'notification_overrides', None) or {}).get(kind) or {})
    except Exception:
        return dict(DEFAULT)
    return out
