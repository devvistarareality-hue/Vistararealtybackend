"""Who may do what, configured per company.

Until now a person's powers came from the text of their designation — "stm" in the
title meant the STM pipeline, "telecaller" meant the calling queue. That made every
company share one set of rules and broke on any title the code didn't know.

Now each company's Designation row carries its own ticked capabilities and a data
scope, so company A's Telecaller and company B's Telecaller can differ. The list of
capabilities lives here in code (a fixed vocabulary, so a typo can't invent a
permission); who holds them is data.

Nothing changed on the day this shipped: a designation with nothing configured
falls back to LEGACY_RULES, the same text matching as before, and the migration
seeds every existing designation from the preset those rules imply.
"""

# ── The vocabulary ───────────────────────────────────────────────────────────
# key, label, module, what it means
CAPABILITIES = [
    ('sales.pipeline.telecalling', 'Works the calling queue', 'Sales',
     'Sees the telecaller pipeline: new leads to call, callbacks, warm transfers.'),
    ('sales.pipeline.stm', 'Works site visits and bookings', 'Sales',
     'Sees the STM pipeline: assigned leads, site visits, closures and bookings.'),
    ('sales.pipeline.cp', 'Works channel-partner leads', 'Sales',
     'A CP Executive: their own partner-sourced leads, inside the Channel Partner module.'),
    ('sales.pipeline.cp_manager', 'Runs the channel-partner desk', 'Sales',
     'Boxed into the Channel Partner module, scoped by assigned projects.'),
    ('sales.lead.assign', 'Assign leads to others', 'Sales',
     'Hand a lead to another telecaller or STM.'),
]
CAPABILITY_KEYS = [c[0] for c in CAPABILITIES]

# ── Data scope: whose records a person sees ──────────────────────────────────
SCOPE_LEGACY = ''          # decided by role and the reporting tree, as before
SCOPE_OWN = 'own'          # only records they own
SCOPE_TEAM = 'team'        # their own and everyone reporting to them
SCOPE_PROJECTS = 'projects'  # every record on the projects assigned to them
SCOPE_COMPANY = 'company'  # everything in the company
DATA_SCOPES = [
    (SCOPE_LEGACY, 'As per role and reporting tree (default)'),
    (SCOPE_OWN, 'Own records only'),
    (SCOPE_TEAM, 'Own records and their team\'s'),
    (SCOPE_PROJECTS, 'Every record on their projects'),
    (SCOPE_COMPANY, 'The whole company'),
]

# ── Presets: what each kind of designation gets by default ───────────────────
PRESETS = {
    'telecaller': ['sales.pipeline.telecalling'],
    'stm': ['sales.pipeline.stm'],
    'cp_executive': ['sales.pipeline.cp'],
    'cp_manager': ['sales.pipeline.cp_manager', 'sales.lead.assign'],
    'sales_desk': ['sales.lead.assign'],   # anyone else in Sales: can hand leads out
}
PRESET_LABELS = {
    'telecaller': 'Telecaller', 'stm': 'Sales Executive (STM)', 'cp_executive': 'CP Executive',
    'cp_manager': 'CP Manager', 'sales_desk': 'Sales desk (assigns leads)',
}

# ── The old text rules, kept as the fallback and as the seed ─────────────────
LEGACY_RULES = [
    ('telecaller', ('telecaller', 'tele caller')),
    ('stm', ('stm', 'sales team', 'sales executive')),
    ('cp_executive', ('cp executive', 'channel partner')),
]


def preset_for_title(title):
    """The preset a designation title implies under the old text rules."""
    t = (title or '').strip().lower()
    if not t:
        return None
    for preset, needles in LEGACY_RULES:
        if any(n in t for n in needles):
            # "CP Cluster Head" and friends: a CP title that isn't an executive
            if preset == 'cp_executive' and t.startswith('cp') and 'executive' not in t:
                return 'cp_manager'
            return preset
    if t.startswith('cp'):
        return 'cp_manager'
    return 'sales_desk'


def legacy_capabilities(title):
    """What the old code would have granted this designation title."""
    caps = set(PRESETS.get(preset_for_title(title)) or [])
    t = (title or '').strip().lower()
    # Telecallers, STMs and CP Executives could never (re)assign leads; everyone
    # else could — including a CP-title manager.
    if not any(k in caps for k in ('sales.pipeline.telecalling', 'sales.pipeline.stm', 'sales.pipeline.cp')):
        caps.add('sales.lead.assign')
    # The old rule for a CP manager looked only at the title starting with "cp"
    # (the role check lives in is_cp_manager), so "CP Executive" carried both.
    if t.startswith('cp'):
        caps.add('sales.pipeline.cp_manager')
    return caps


def _designation_row(user):
    """The user's designation in their company's master, matched by name."""
    title = (getattr(user, 'designation', '') or '').strip()
    company_id = getattr(user, 'company_id', None)
    if not (title and company_id):
        return None
    from .models import Designation
    return (Designation.objects.filter(company_id=company_id, name__iexact=title)
            .order_by('id').first())


def capabilities_for(user):
    """Everything this user may do: their designation's ticks (or the old rules
    when it has none), plus their own extras, minus anything taken away."""
    if user is None or not getattr(user, 'is_authenticated', True):
        return set()
    cached = getattr(user, '_capabilities_cache', None)
    if cached is not None:
        return cached
    row = _designation_row(user)
    if row is not None and row.capabilities_set:
        caps = set(row.capabilities or [])
    else:
        caps = legacy_capabilities(getattr(user, 'designation', ''))
    caps |= set(getattr(user, 'extra_capabilities', None) or [])
    caps -= set(getattr(user, 'denied_capabilities', None) or [])
    caps &= set(CAPABILITY_KEYS)
    try:
        user._capabilities_cache = caps
    except Exception:
        pass
    return caps


def user_can(user, key):
    return key in capabilities_for(user)


def data_scope(user):
    """The scope configured on the user's designation, or '' to keep the old
    role/reporting-tree behaviour."""
    row = _designation_row(user)
    return (row.data_scope if row is not None else '') or SCOPE_LEGACY
