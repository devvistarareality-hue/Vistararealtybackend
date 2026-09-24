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
    ('sales.pipeline.cp', 'Works channel-partner leads', 'Channel Partner',
     'A CP Executive: their own partner-sourced leads.'),
    ('sales.pipeline.cp_manager', 'Runs the channel-partner desk', 'Channel Partner',
     'The whole partner desk, scoped by assigned projects.'),
    ('sales.lead.assign', 'Assign leads to others', 'Sales',
     'Hand a lead to another telecaller or STM.'),
    # Actions inside a module the person already has. Everyone with the module could
    # do these before, so they start ticked and a company can untick them.
    ('ar.receipt.record', 'Record receipts', 'AR',
     'Enter a payment against an account.'),
    ('ar.receipt.edit', 'Edit or delete receipts', 'AR',
     'Correct or remove a payment already entered.'),
    ('ar.import.run', 'Import receipts from Excel', 'AR',
     'Upload the receipts template.'),
    ('ar.followup.manage', 'Schedule and close collection follow-ups', 'AR',
     'Book a follow-up, assign it, and record what the customer said.'),
    ('ar.legal_date.set', 'Set the Legal & Other due date', 'AR',
     'Decides when interest starts on that line.'),
    ('club.investor.manage', 'Add and edit investors', 'Club 1000',
     'Create an investor, revise, renew or redeem.'),
    ('club.payout.mark_paid', 'Mark payouts and rewards paid', 'Club 1000',
     'Close out a payout or a referral reward.'),
]

# ── Screens: which menu items a designation sees ─────────────────────────────
# Leaving a designation's screens unset keeps today's behaviour (the menu follows
# role and pipeline). Ticking any makes the list explicit for that designation.
SCREENS = [
    ('sales.screen.dashboard', 'Dashboard', 'Sales'),
    ('sales.screen.leads', 'All Leads', 'Sales'),
    ('sales.screen.followups', 'Follow-Ups', 'Sales'),
    ('sales.screen.sitevisits', 'Site Visits', 'Sales'),
    ('sales.screen.booking', 'Booking', 'Sales'),
    ('sales.screen.myteam', 'My Team', 'Sales'),
    ('sales.screen.approvals', 'Approvals', 'Sales'),
    ('sales.screen.import', 'Import Leads', 'Sales'),
    ('sales.screen.reports', 'Reports', 'Sales'),
    ('sales.screen.projects', 'Projects', 'Sales'),
    ('sales.screen.leadsetup', 'Lead Setup', 'Sales'),
    ('sales.screen.teamusers', 'Team Users', 'Sales'),
    ('sales.screen.distribution', 'Distribution', 'Sales'),
    ('sales.screen.datareset', 'Data Reset', 'Sales'),
    # The Channel Partner module has its own menu, so it gets its own keys — a
    # CP Cluster Head's whole sidebar is this list.
    ('cp.screen.dashboard', 'Dashboard', 'Channel Partner'),
    ('cp.screen.leads', 'All Leads', 'Channel Partner'),
    ('cp.screen.sitevisits', 'Site Visits', 'Channel Partner'),
    ('cp.screen.followups', 'Follow-Ups', 'Channel Partner'),
    ('cp.screen.booking', 'Booking', 'Channel Partner'),
    ('cp.screen.myteam', 'My Team', 'Channel Partner'),
    ('cp.screen.approvals', 'Approvals', 'Channel Partner'),
    # Every module has the same three tabs — Dashboard, My Team, and Log for
    # admins — plus whatever else it does.
    ('accounts.screen.dashboard', 'Dashboard', 'Accounts & Finance'),
    ('accounts.screen.myteam', 'My Team', 'Accounts & Finance'),
    ('accounts.screen.approvals', 'Approvals', 'Accounts & Finance'),
    ('accounts.screen.bookings', 'Bookings', 'Accounts & Finance'),
    ('hr.screen.dashboard', 'Dashboard', 'HR'),
    ('hr.screen.myteam', 'My Team', 'HR'),
    ('execution.screen.dashboard', 'Dashboard', 'Execution'),
    ('execution.screen.myteam', 'My Team', 'Execution'),
    ('purchase.screen.dashboard', 'Dashboard', 'Purchase'),
    ('purchase.screen.myteam', 'My Team', 'Purchase'),
    ('land.screen.dashboard', 'Dashboard', 'Land'),
    ('land.screen.myteam', 'My Team', 'Land'),
    ('ar.screen.dashboard', 'Dashboard', 'AR'),
    ('ar.screen.myteam', 'My Team', 'AR'),
    ('ar.screen.collections', 'Collections', 'AR'),
    ('ar.screen.register', 'Register', 'AR'),
    ('ar.screen.import', 'Import receipts', 'AR'),
    ('club.screen.dashboard', 'Dashboard', 'Club 1000'),
    ('club.screen.leads', 'Leads', 'Club 1000'),
    ('club.screen.followups', 'Follow-Ups', 'Club 1000'),
    ('club.screen.investors', 'Investors', 'Club 1000'),
    ('club.screen.schemes', 'Schemes', 'Club 1000'),
    ('club.screen.payouts', 'Payouts', 'Club 1000'),
    ('club.screen.rewards', 'Referral Rewards', 'Club 1000'),
    ('club.screen.approvals', 'Approvals', 'Club 1000'),
    ('club.screen.myteam', 'My Team', 'Club 1000'),
]
SCREEN_KEYS = [s[0] for s in SCREENS]
# Which module each menu item belongs to, for deciding whether a saved menu
# speaks for it at all. See screen_modules_for.
SCREEN_MODULE = {k: m for k, _, m in SCREENS}

# Which screens each preset ticks when a company first sets one up — the menu
# those people see today.
PRESET_SCREENS = {
    'telecaller': ['sales.screen.dashboard', 'sales.screen.leads', 'sales.screen.followups',
                   'sales.screen.import', 'sales.screen.reports'],
    'stm': ['sales.screen.dashboard', 'sales.screen.leads', 'sales.screen.followups',
            'sales.screen.sitevisits', 'sales.screen.booking', 'sales.screen.import', 'sales.screen.reports'],
    'cp_executive': ['cp.screen.dashboard', 'cp.screen.leads', 'cp.screen.sitevisits',
                     'cp.screen.followups', 'cp.screen.booking',
                     'cp.screen.approvals'],
    'cp_manager': [k for k, _, m in SCREENS if m == 'Channel Partner'],
    'sales_desk': [k for k, _, _ in SCREENS],
}

# ── Which dashboard opens ────────────────────────────────────────────────────
DASHBOARD_AUTO = ''
# The roles a dashboard can be written for — the same list User Management uses
# when it creates a person, minus Kiosk (which has its own locked screen). Each
# module's Dashboard has a role filter over these; Designation → Permissions then
# pins the one that opens for a designation.
DASHBOARD_ROLES = ['Director', 'General Manager', 'Manager', 'Employee', 'Intern']

# The dashboards that exist, per module, per role. Sales and Channel Partner use
# the views that were there before permissions: the telecaller queue, the STM's
# own pipeline and the manager/director desk.
# (value, label, module, role)
DASHBOARDS = [
    (DASHBOARD_AUTO, 'Decide from their permissions (default)', '', ''),
    # Sales
    ('telecaller', 'Telecaller — the call queue', 'Sales', 'Employee'),
    ('stm', 'Sales Executive — their own pipeline', 'Sales', 'Employee'),
    ('manager', 'Manager — the whole desk', 'Sales', 'Manager'),
    ('gm', 'General Manager — the whole desk', 'Sales', 'General Manager'),
    ('director', 'Director — company-wide figures', 'Sales', 'Director'),
    # Channel Partner — the same views, scoped to partner-sourced records.
    ('cp_exec', 'CP Executive — their own partner leads', 'Channel Partner', 'Employee'),
    ('cp_manager', 'CP Manager — the partner desk', 'Channel Partner', 'Manager'),
    ('cp_gm', 'General Manager — the partner desk', 'Channel Partner', 'General Manager'),
    ('cp_director', 'Director — every partner, every project', 'Channel Partner', 'Director'),
    # Club 1000 — the Manager and Executive dashboards it already has.
    ('club_exec', 'Executive — their own investors', 'Club 1000', 'Employee'),
    ('club_manager', 'Manager — the investment desk', 'Club 1000', 'Manager'),
]

# The modules whose dashboard is one view for everyone: the same dashboard is
# offered to every role, so a company can pin it to any designation. (AR's
# receivables dashboard, Accounts & Finance's, and the plain one the rest open.)
_SHARED_DASHBOARDS = [
    ('ar', 'AR', 'the receivables book'),
    ('accounts', 'Accounts & Finance', 'the approvals desk'),
    ('hr', 'HR', 'the department'),
    ('execution', 'Execution', 'the site desk'),
    ('purchase', 'Purchase', 'the buying desk'),
    ('land', 'Land', 'the land desk'),
]
for _pfx, _module, _what in _SHARED_DASHBOARDS:
    for _role in DASHBOARD_ROLES:
        DASHBOARDS.append((f'{_pfx}_{_role.lower().replace(" ", "_")}',
                           f'{_role} — {_what}', _module, _role))

DASHBOARD_KEYS = [d[0] for d in DASHBOARDS]

# Granted to everyone by default, because before capabilities anyone with the
# module could already do them. Ticking stays with the company to remove.
DEFAULT_ON = [
    'ar.receipt.record', 'ar.receipt.edit', 'ar.import.run', 'ar.followup.manage',
    'ar.legal_date.set', 'club.investor.manage', 'club.payout.mark_paid',
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
# Which module each preset is for — Channel Partner is its own module, so a Sales
# designation should never be offered the CP ones, or the other way round.
PRESET_MODULES = {
    'telecaller': 'Sales', 'stm': 'Sales', 'sales_desk': 'Sales',
    'cp_executive': 'Channel Partner', 'cp_manager': 'Channel Partner',
}

# ── The old text rules, kept as the fallback and as the seed ─────────────────
LEGACY_RULES = [
    ('telecaller', ('telecaller', 'tele caller')),
    ('stm', ('stm', 'sales team', 'sales executive')),
    ('cp_executive', ('cp executive', 'channel partner')),
]


# Which modules a designation of this module is allowed to decide. A Sales
# designation covers the Channel Partner module too — CP lives inside Sales.
MODULE_FAMILY = {
    'Sales': ('Sales',),
    'Channel Partner': ('Channel Partner',),
    'Accounts & Finance': ('Accounts & Finance',),
    'AR': ('AR',),
    'Accounts Receivable': ('AR',),
    'Club 1000': ('Club 1000',),
}


# Every module the system knows, in the order the launcher shows them. Mirrors
# ALL_MODULES in the web app's lib/moduleAccess.js.
ALL_MODULES = ['Sales', 'Channel Partner', 'HR', 'Accounts & Finance', 'AR',
               'Execution', 'Purchase', 'Land', 'Club 1000']


def modules_of(module):
    """The modules a designation in `module` decides. Unknown module → just itself."""
    return MODULE_FAMILY.get(module or '', (module,) if module else ())


def keys_in_modules(rows, modules):
    """The keys from CAPABILITIES/SCREENS that belong to these modules."""
    wanted = set(modules or ())
    return {r[0] for r in rows if r[2] in wanted}


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


def legacy_capabilities(title, module=None):
    """What the old code would have granted this designation title.

    `module` limits the answer to that designation's own module: a Sales
    designation says nothing about AR, so the editor never shows AR ticked for
    it. Left out (the whole vocabulary) for the runtime check, where module
    access is the gate anyway.
    """
    caps = set(PRESETS.get(preset_for_title(title)) or []) | set(DEFAULT_ON)
    t = (title or '').strip().lower()
    # Telecallers, STMs and CP Executives could never (re)assign leads; everyone
    # else could — including a CP-title manager.
    if not any(k in caps for k in ('sales.pipeline.telecalling', 'sales.pipeline.stm', 'sales.pipeline.cp')):
        caps.add('sales.lead.assign')
    # The old rule for a CP manager looked only at the title starting with "cp"
    # (the role check lives in is_cp_manager), so "CP Executive" carried both.
    if t.startswith('cp'):
        caps.add('sales.pipeline.cp_manager')
    if module:
        caps &= keys_in_modules(CAPABILITIES, modules_of(module))
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
        # A Sales designation decides Sales, not AR or Club 1000: outside its own
        # module the person keeps what everyone with that module always had.
        own = keys_in_modules(CAPABILITIES, modules_of(row.module))
        caps |= (set(DEFAULT_ON) - own)
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


def _row_for(user):
    return _designation_row(user)


def screens_for(user):
    """The menu this person sees, or None to keep today's role-based menu."""
    row = _designation_row(user)
    if row is None or not row.screens_set:
        return None
    return set(row.screens or [])


def screen_modules_for(user):
    """The modules the saved menu speaks for, or None when there is no saved menu.

    A designation belongs to one module, but the people holding it may be granted
    others — a CFO with Accounts & Finance, Sales and Land. Ticking an Accounts
    menu used to empty Sales and Land as well, because a saved menu governed
    every module at once. Now it governs only the modules it was configured for,
    and the rest keep their default menu.

    Older rows carry nothing here, so they fall back to the modules their own
    keys name — which is the module the designation was configured in.
    """
    row = _designation_row(user)
    if row is None or not row.screens_set:
        return None
    named = set(getattr(row, 'screens_modules', None) or [])
    if not named:
        named = {SCREEN_MODULE[k] for k in (row.screens or []) if k in SCREEN_MODULE}
        named |= set(modules_of(row.module))
    return named


def can_see_screen(user, key):
    allowed = screens_for(user)
    if allowed is None:
        return True
    # A menu item whose module this designation was never configured for is left
    # alone: the person keeps that module's default menu.
    module = SCREEN_MODULE.get(key)
    if module and module not in (screen_modules_for(user) or ()):
        return True
    return key in allowed


def permissions_version(company_id):
    """A number that changes whenever this company's permissions change.

    Cached figures (the Sales dashboard) put it in their key, so an admin who
    changes a designation sees the effect at once instead of waiting for the
    cache to expire."""
    from django.core.cache import cache
    return cache.get(f'perm_ver:{company_id}', 0)


def bump_permissions_version(company_id):
    from django.core.cache import cache
    key = f'perm_ver:{company_id}'
    try:
        cache.incr(key)
    except ValueError:            # not set yet
        cache.set(key, 1, timeout=None)


def role_dashboards(user):
    """What this person's role opens in each module, as set by the Copy button on
    a module's Dashboard: {'Sales': 'manager', 'AR': 'ar_manager', …}. Their
    designation's own pin still wins over this."""
    company_id = getattr(user, 'company_id', None)
    role = getattr(user, 'role', '') or ''
    if not (company_id and role):
        return {}
    from .models import RoleDashboard
    return {r.module: r.view for r in
            RoleDashboard.objects.filter(company_id=company_id, role=role)}


def dashboard_for(user):
    """Which dashboard to open: '' means decide from their permissions."""
    row = _designation_row(user)
    return (row.dashboard if row is not None else '') or DASHBOARD_AUTO


CP_MENU_KEYS = [k for k, _, m in SCREENS if m == 'Channel Partner']


def with_module_defaults(screens):
    """Tidy a menu before it is saved.

    Channel Partner is its own module, so a menu saved for it that names none of
    its tabs (an older browser, a stale tab) would leave that person with no
    sidebar at all. Their own module's tabs come back.
    """
    keys = set(screens or [])
    if 'sales.screen.cp' in keys:
        keys.discard('sales.screen.cp')          # the Sales menu no longer has it
        keys |= set(CP_MENU_KEYS)
    return sorted(keys)


def preset_screens(title, module=None):
    """The menu a title implies, used to pre-tick the editor. `module` limits it
    to that designation's own module, as legacy_capabilities does."""
    keys = set(PRESET_SCREENS.get(preset_for_title(title)) or PRESET_SCREENS['sales_desk'])
    if module:
        family = modules_of(module)
        # A module with no preset of its own starts with all of its screens —
        # that is what its people see today.
        keys = (keys | keys_in_modules(SCREENS, family)) & keys_in_modules(SCREENS, family) \
            if module not in ('Sales', 'Channel Partner') else keys & keys_in_modules(SCREENS, family)
    return sorted(keys)
