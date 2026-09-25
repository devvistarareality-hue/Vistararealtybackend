"""Excel backup of one company: a workbook with a sheet per module, and the
restore that reads it back.

Built around one workflow. An admin takes a snapshot, runs Data Reset
(SalesDataResetView) to wipe that company's trial Sales data, then uploads the
same file to put it back — with the original row ids, so everything that
pointed at a record still resolves.

Only the tables Data Reset actually clears are restorable. The rest (projects,
plots, users, AR, tasks, Club 1000 …) are exported so the snapshot is complete,
but never written back: the reset never removed them, so there is nothing to
put back and overwriting live rows is exactly what we don't want.

Encrypted columns need no special handling here. EncryptedTextField and friends
(see fields.py) decrypt on read and encrypt on write, so reading an attribute
gives plaintext for the sheet and writing plaintext back re-encrypts it.

A workbook only ever goes back into the company it came from. The request being
scoped to one company decides which file you get, not which rows get written —
so restore checks the rows themselves and refuses a file holding anyone else's.
"""
import json
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

import openpyxl
from django.apps import apps
from django.db import models, transaction
from django.utils import timezone
from openpyxl.styles import Alignment, Font, PatternFill

NAVY = 'FF0F1838'

# Never written to a sheet. Password hashes and session tokens would be a
# liability sitting in a downloaded file, and the email blind index is a
# derived lookup key nobody reading a backup needs.
SENSITIVE = {'password', 'session_token_app', 'session_token_web', 'email_key'}

# Marks the readable half of a foreign key pair. Restore skips these columns —
# the id column next to it is the real value.
LABEL_SUFFIX = ' (name)'


class Table:
    """One stacked table inside a module sheet."""

    def __init__(self, label, model, scope, restorable=False):
        self.label = label
        self.model = model        # 'app_label.ModelName'
        self.scope = scope        # ORM path from this model to companies.Company
        self.restorable = restorable

    @property
    def cls(self):
        return apps.get_model(self.model)


# Sheet -> the tables stacked inside it. Within a sheet the order is also the
# order restore writes them in, so a table never refers to one below it:
# closures before bookings (Booking.closure), leads before both.
SHEETS = [
    ('Sales', [
        # Restore writes these in this order, so it follows the pipeline: a lead,
        # then its visits, then the closure that can name one, then the booking
        # that names the closure.
        Table('Leads', 'sales.Lead', 'company', restorable=True),
        Table('Site Visits', 'sales.SiteVisit', 'lead__company', restorable=True),
        Table('Closures', 'sales.Closure', 'company', restorable=True),
        Table('Bookings', 'sales.Booking', 'company', restorable=True),
        Table('Follow-Ups', 'sales.FollowUp', 'lead__company', restorable=True),
        Table('Lead History', 'sales.LeadStatusHistory', 'lead__company', restorable=True),
        Table('Distribution Log', 'sales.DistributionLog', 'company', restorable=True),
        Table('Availability', 'sales.UserAvailability', 'user__company', restorable=True),
        Table('Notifications', 'accounts.Notification', 'recipient__company', restorable=True),
        # Data Reset never names lead transfers, but LeadTransfer.lead is CASCADE,
        # so wiping leads takes them with it — restorable or they are lost for good.
        Table('Lead Transfers', 'sales.LeadTransfer', 'company', restorable=True),
        # Data Reset keeps these, so they are snapshot-only.
        Table('Lead Sources', 'sales.LeadSource', 'company'),
        Table('Projects', 'sales.Project', 'company'),
        Table('Plots', 'sales.Plot', 'project__company'),
        Table('Project Assignments', 'sales.UserProjectAssignment', 'user__company'),
        Table('Sales Team', 'sales.SalesTeamMember', 'user__company'),
        Table('Distribution Settings', 'sales.DistributionSettings', 'company'),
        Table('Distribution Weights', 'sales.UserDistributionWeight', 'user__company'),
        Table('Meta Form Mappings', 'sales.MetaFormMapping', 'company'),
        Table('Meta Webhook Config', 'sales.MetaWebhookConfig', 'company'),
    ]),
    ('Channel Partner', [
        Table('Channel Partners', 'sales.ChannelPartner', 'company'),
    ]),
    ('HR', [
        Table('Users', 'accounts.User', 'company'),
        Table('Designations', 'accounts.Designation', 'company'),
        Table('Role Dashboards', 'accounts.RoleDashboard', 'company'),
        Table('Attendance', 'attendance.AttendanceRecord', 'user__company'),
        Table('Leave Applications', 'attendance.LeaveApplication', 'user__company'),
        Table('Leave Balances', 'attendance.LeaveBalance', 'user__company'),
        Table('Leave Transactions', 'attendance.LeaveTransaction', 'user__company'),
    ]),
    ('AR', [
        Table('AR Accounts', 'receivables.ARAccount', 'company'),
        Table('AR Receipts', 'receivables.ARReceipt', 'account__company'),
        Table('AR Follow-Ups', 'receivables.ARFollowUp', 'account__company'),
        Table('AR Receipt Audit', 'receivables.ARReceiptAudit', 'receipt__account__company'),
    ]),
    ('Task Allocation', [
        Table('Task Lists', 'tasks.TaskList', 'company'),
        Table('Task Tags', 'tasks.TaskTag', 'company'),
        Table('Tasks', 'tasks.Task', 'company'),
        Table('Task Checklist', 'tasks.TaskChecklistItem', 'task__company'),
        Table('Task Comments', 'tasks.TaskComment', 'task__company'),
    ]),
    ('Club 1000', [
        Table('Club Leads', 'club1000.Lead', 'company'),
        Table('Club Lead History', 'club1000.LeadStatusHistory', 'lead__company'),
        Table('Club Follow-Ups', 'club1000.FollowUp', 'lead__company'),
        Table('Schemes', 'club1000.Scheme', 'company'),
        Table('Investors', 'club1000.Investor', 'company'),
        Table('Payouts', 'club1000.Payout', 'investor__company'),
        Table('Referral Rewards', 'club1000.ReferralReward', 'investor__company'),
    ]),
    ('Activity Log', [
        Table('Activity Log', 'activity.ActivityLog', 'company'),
    ]),
]

# A sheet is a module: what you pick when backing up or resetting part of a company.
MODULES = [name for name, _ in SHEETS]
TABLES_BY_MODULE = {name: list(tables) for name, tables in SHEETS}
MODULE_OF = {t.model: name for name, tables in SHEETS for t in tables}

ALL_TABLES = [t for _, tables in SHEETS for t in tables]
# Everything is restorable: the workbook has to be able to rebuild a company
# from nothing, not just undo a Data Reset.
RESTORABLE = ALL_TABLES
ALL_LABELS = {t.label for t in ALL_TABLES}


def pick_modules(modules):
    """The requested modules, or all of them. Unknown names are dropped."""
    if not modules:
        return list(MODULES)
    wanted = [m for m in MODULES if m in set(modules)]
    return wanted or list(MODULES)


# A child with one of these either dies with its parent or refuses to let the
# parent go. Either way it has to be part of the reset, and part of the backup
# that authorises it. SET_NULL children simply survive with an empty column.
_PULLS_IN = {'CASCADE', 'PROTECT', 'RESTRICT'}


def cascade_modules(modules):
    """The modules a reset of `modules` actually destroys.

    Ticking a module is rarely the whole story. HR holds the users, and a user's
    departure takes their follow-ups in Sales and Club 1000 with it. Sales holds
    the bookings, and AR accounts PROTECT those — so Sales cannot go without AR
    going first. Both cases mean the same thing: the honest list of what will be
    lost is wider than what was ticked, and it is that list the backup gate has
    to cover and the screen has to show.
    """
    # A module-level fixpoint, not a table-level one: a reset empties whole
    # modules, so the moment any of Sales is doomed every Sales table goes —
    # bookings included — and that is what drags AR in behind it.
    chosen = set(pick_modules(modules))
    while True:
        doomed = {t.model for m in chosen for t in TABLES_BY_MODULE[m]}
        pulled = set()
        for table in ALL_TABLES:
            if table.model in doomed:
                continue
            for f in table.cls._meta.fields:
                if not (f.is_relation and f.related_model):
                    continue
                on_delete = getattr(getattr(f, 'remote_field', None), 'on_delete', None)
                if (getattr(on_delete, '__name__', '') in _PULLS_IN
                        and f.related_model._meta.label in doomed):
                    pulled.add(MODULE_OF[table.model])
                    break
        if pulled <= chosen:
            return [m for m in MODULES if m in chosen]
        chosen |= pulled


# ── Columns ──────────────────────────────────────────────────────────────────
def _columns(model):
    """[(header, field, kind)] for one model.

    A foreign key gets two columns: the raw id, which is what restore reads,
    and a readable label beside it, which is what a person reading the sheet
    wants. Many-to-many fields are not in _meta.fields and are not exported —
    none of the restorable tables has one.
    """
    cols = []
    for f in model._meta.fields:
        if f.name in SENSITIVE:
            continue
        if f.many_to_one or f.one_to_one:
            cols.append((f.attname, f, 'id'))
            cols.append((f.name + LABEL_SUFFIX, f, 'label'))
        else:
            cols.append((f.name, f, 'value'))
    return cols


def _cell(value):
    """Coerce a Python value into something openpyxl will write."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        # ISO text, not an Excel date cell. Excel keeps dates as floating-point
        # days, which rounds a timestamp to the nearest millisecond and loses the
        # offset — a backup that comes back a few microseconds off is not the
        # same backup. ISO keeps both exactly, reads fine, and still sorts
        # chronologically because it is zero-padded most-significant-first.
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, date):
        return value          # a whole day survives Excel's date cell exactly
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


# ── Export ───────────────────────────────────────────────────────────────────
# A readable label beside each foreign key is worth having, but asking each row
# for str(related) is not: every call wakes the related object up and costs a
# query, which measured at ~110ms per row — hours over a full export. So the
# labels for a related table are fetched once, into {pk: label}, and looked up.
# Past LABEL_LIMIT rows that stops being worth it (and the big ones are things
# like Lead.duplicate_of pointing back at every lead), so the id stands alone.
LABEL_LIMIT = 3000


def _label_maps(cols, cache):
    """{field name: {pk: label}} for the foreign keys worth labelling."""
    maps = {}
    for _, f, kind in cols:
        if kind != 'label':
            continue
        target = f.related_model
        if target not in cache:
            cache[target] = ({o.pk: str(o) for o in target.objects.all()}
                             if target.objects.count() <= LABEL_LIMIT else None)
        maps[f.name] = cache[target]
    return maps


def _styled(ws, values, font, fill=None):
    """A row of write-only cells carrying their own styling."""
    from openpyxl.cell import WriteOnlyCell
    out = []
    for v in values:
        cell = WriteOnlyCell(ws, value=v)
        cell.font = font
        if fill:
            cell.fill = fill
            cell.alignment = Alignment(horizontal='center', vertical='center')
        out.append(cell)
    return out


def _append_table(ws, table, company, cache):
    cols = _columns(table.cls)
    maps = _label_maps(cols, cache)
    qs = table.cls.objects.filter(**{table.scope: company}).order_by('pk')
    count = qs.count()

    ws.append(_styled(ws, [table.label], Font(bold=True, size=12, color=NAVY)))
    ws.append(_styled(ws, [f'{count} row{"" if count == 1 else "s"}'],
                      Font(italic=True, size=9, color='FF6B7280')))
    ws.append(_styled(ws, [h for h, _, _ in cols],
                      Font(bold=True, color='FFFFFFFF', size=10),
                      PatternFill('solid', fgColor=NAVY)))

    # .iterator() so a table with six figures of rows never lands in memory at once.
    for obj in qs.iterator(chunk_size=2000):
        line = []
        for _, f, kind in cols:
            if kind == 'id':
                line.append(getattr(obj, f.attname, None))
            elif kind == 'label':
                m = maps.get(f.name)
                line.append(m.get(getattr(obj, f.attname, None)) if m else None)
            else:
                line.append(_cell(getattr(obj, f.name, None)))
        ws.append(line)

    ws.append([])   # blank spacer closes the table for the restore parser


def build_workbook(company, modules=None):
    """A workbook of what this company owns, a sheet per module.

    write_only so the rows stream out instead of being held as cell objects —
    a full company runs to a couple of hundred thousand rows.
    """
    chosen = set(pick_modules(modules))
    wb = openpyxl.Workbook(write_only=True)
    cache = {}
    for sheet_name, tables in SHEETS:
        if sheet_name not in chosen:
            continue
        ws = wb.create_sheet(sheet_name[:31])   # Excel caps sheet names at 31
        ws.append(_styled(ws, [f'{sheet_name} — {company.name}'],
                          Font(bold=True, size=14, color=NAVY)))
        ws.append(_styled(ws, [f'Backup taken {timezone.localtime().strftime("%d %b %Y, %I:%M %p")}'],
                          Font(italic=True, size=9, color='FF6B7280')))
        ws.append([])
        for table in tables:
            _append_table(ws, table, company, cache)
    return wb


# ── Restore ──────────────────────────────────────────────────────────────────
def parse_workbook(fileobj):
    """{table label: [row dicts]} for the restorable tables found in the file.

    Every table starts with its label alone in column A. Data rows always start
    with a numeric id, so a string there can only be a title or a header — which
    is what makes finding the sections reliable.
    """
    wb = openpyxl.load_workbook(fileobj, read_only=True, data_only=True)
    wanted = {t.label for t in RESTORABLE}
    found, label, headers = {}, None, None
    try:
        for ws in wb.worksheets:
            label, headers = None, None
            for row in ws.iter_rows(values_only=True):
                first = row[0] if row else None
                if isinstance(first, str) and first.strip() in ALL_LABELS:
                    label = first.strip()
                    headers = None
                    if label in wanted:
                        found.setdefault(label, [])
                    continue
                if label is None:
                    continue
                if not any(c is not None and c != '' for c in row):
                    label, headers = None, None     # blank row ends the table
                    continue
                if headers is None:
                    # The row count line sits between the title and the header.
                    if isinstance(first, str) and first.endswith(('row', 'rows')):
                        continue
                    headers = [str(c).strip() if c is not None else '' for c in row]
                    continue
                if label in wanted:
                    found[label].append(dict(zip(headers, row)))
    finally:
        wb.close()
    return found


def _empty_for(field):
    """What an empty cell means for this column.

    A blank-but-not-null column holds '' in the database, never NULL, and Excel
    gives back None for both — so handing None straight to a NOT NULL column is
    how a restore fails halfway. Fall back to the column's own idea of empty.
    """
    if field.null:
        return None
    if field.has_default():
        return field.get_default()
    if isinstance(field, (models.CharField, models.TextField)):
        return ''
    return None


def _to_python(field, value):
    """Turn a cell back into what the field expects."""
    if value is None or value == '':
        return _empty_for(field)
    if isinstance(field, models.DateTimeField):        # before DateField: it subclasses it
        if isinstance(value, datetime):
            dt = value                                  # an Excel date cell, from an older file
        elif isinstance(value, date):
            dt = datetime(value.year, value.month, value.day)
        else:
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(str(value).strip())
            if dt is None:
                return None
        # Exported ISO carries its offset, so this normally keeps the exact
        # original instant; a naive value (older file) is read as local time.
        return timezone.make_aware(dt) if timezone.is_naive(dt) else dt
    if isinstance(field, models.DateField):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        from django.utils.dateparse import parse_date
        return parse_date(str(value))
    if isinstance(field, models.TimeField):
        if isinstance(value, time):
            return value
        if isinstance(value, datetime):
            return value.time()
        from django.utils.dateparse import parse_time
        return parse_time(str(value))
    if isinstance(field, models.DecimalField):         # covers EncryptedDecimalField
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        # Excel hands back a bare number, so 2500000.00 arrives as 2500000 and
        # would be stored — and shown — a digit short. Put the scale back.
        if field.decimal_places is not None:
            d = d.quantize(Decimal(1).scaleb(-field.decimal_places))
        return d
    if isinstance(field, models.BooleanField):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ('1', 'true', 'yes', 'y')
    if isinstance(field, models.JSONField):
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except (ValueError, TypeError):
            return {}
    if isinstance(field, (models.IntegerField, models.AutoField)):
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    if isinstance(field, models.FloatField):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    if isinstance(field, models.UUIDField):
        try:
            return uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None
    return str(value)


@contextmanager
def _keep_original_timestamps(model_classes):
    """Let auto_now / auto_now_add columns keep the values from the sheet.

    Django stamps those fields on write, which would silently replace every
    created_at with the moment of the restore — the one thing a restore must
    not do. Flipping the flags off is process-wide for the duration, so a
    concurrent write would also miss its stamp; acceptable here because this
    runs inside one short admin-only transaction.
    """
    touched = []
    for model in model_classes:
        for f in model._meta.fields:
            if getattr(f, 'auto_now', False) or getattr(f, 'auto_now_add', False):
                touched.append((f, f.auto_now, f.auto_now_add))
                f.auto_now = f.auto_now_add = False
    try:
        yield
    finally:
        for f, auto_now, auto_now_add in touched:
            f.auto_now, f.auto_now_add = auto_now, auto_now_add




def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def restore_order():
    """Every table, ordered so nothing is written before what it points at.

    Worked out from the models rather than the order the sheets happen to be
    written in, because one wrong hand-maintained position is an insert that
    fails halfway through a restore. There are no cycles among these tables; a
    self-reference (Lead.duplicate_of) is not an ordering problem.
    """
    covered = {t.model: t for t in ALL_TABLES}
    deps = {}
    for label, table in covered.items():
        needs = set()
        for f in table.cls._meta.fields:
            if f.is_relation and f.related_model:
                target = f.related_model._meta.label
                if target in covered and target != label:
                    needs.add(target)
        deps[label] = needs

    order, done, visiting = [], set(), set()

    def visit(label):
        if label in done or label in visiting:
            return
        visiting.add(label)
        for parent in sorted(deps[label]):
            visit(parent)
        visiting.discard(label)
        done.add(label)
        order.append(covered[label])

    for label in sorted(deps):
        visit(label)
    return order


def _parent_field(table):
    """The foreign key a table's scope path hangs off, e.g. 'lead' for
    'lead__company'. None when the table has its own company column."""
    return None if table.scope == 'company' else table.scope.split('__')[0]


def _ownership(company, parsed):
    """({table label: set of ids that are this company's}, [rows that are not]).

    A workbook only goes back into the company it came from, and the request
    being scoped to one company decides which file you get, not which rows get
    written — so the rows are checked themselves.

    A row belongs here if its own company column says so, or if the parent it
    hangs off does. The parent may be in this same workbook (a full restore
    writes both) or already in the database (restoring after a partial wipe),
    so both count. Walking in dependency order means a parent is always settled
    before anything pointing at it is judged.
    """
    valid, problems = {}, []
    for table in restore_order():
        rows = parsed.get(table.label) or []
        field = _parent_field(table)
        parent_ids = None
        if field:
            parent_label = table.cls._meta.get_field(field).related_model._meta.label
            parent_ids = valid.get(parent_label, set())

        mine, bad = set(), 0
        for row in rows:
            row_id = _as_int(row.get('id'))
            if field is None:
                own = str(row.get('company_id') or '') == str(company.pk)
                # Booking.company is nullable; fall back to the lead it belongs to.
                if not own and not row.get('company_id'):
                    own = _as_int(row.get('lead_id')) in valid.get('sales.Lead', set())
            else:
                own = _as_int(row.get(f'{field}_id')) in parent_ids
            if own:
                mine.add(row_id)
            else:
                bad += 1
        if bad:
            problems.append({'table': table.label, 'rows': bad})

        # Rows already in the database count as this company's too, so a child
        # pointing at something that survived the wipe is not called foreign.
        existing = set(table.cls.objects.filter(**{table.scope: company})
                       .values_list('pk', flat=True))
        valid[table.model] = mine | existing
    return valid, problems


def restore(company, parsed, commit=False):
    """Write back everything in the workbook that is missing, or report what would be.

    Rows whose id is already taken are skipped, never overwritten: a restore
    fills gaps, it does not decide that the file is more current than the
    database. That is what lets one workbook serve both jobs — putting back a
    company wiped to the ground, and undoing a Data Reset that cleared only the
    sales tables and left users, projects and plots in place.

    Refuses outright if the file holds another company's rows.
    """
    order = restore_order()
    valid, foreign = _ownership(company, parsed)

    plan, total_new, total_skip = [], 0, 0
    for table in order:
        rows = parsed.get(table.label) or []
        ids = [i for i in (_as_int(r.get('id')) for r in rows) if i is not None]
        existing = set(table.cls.objects.filter(pk__in=ids).values_list('pk', flat=True)) if ids else set()
        new = len(ids) - len(existing)
        if rows:
            plan.append({'table': table.label, 'rows': len(rows),
                         'restore': new, 'already_there': len(existing)})
        total_new += new
        total_skip += len(existing)

    if foreign:
        return {'ok': False, 'committed': False, 'plan': plan, 'total': total_new,
                'already_there': total_skip, 'conflicts': [], 'foreign': foreign,
                'detail': f'This workbook holds records belonging to another company, so it '
                          f'cannot be restored into {company.name}. Nothing was written.'}
    if not commit:
        return {'ok': True, 'committed': False, 'plan': plan, 'total': total_new,
                'already_there': total_skip, 'conflicts': [], 'foreign': []}

    User = apps.get_model('accounts.User')
    with _keep_original_timestamps([t.cls for t in order]), transaction.atomic():
        for table in order:
            rows = parsed.get(table.label) or []
            if not rows:
                continue
            model = table.cls
            ids = [i for i in (_as_int(r.get('id')) for r in rows) if i is not None]
            taken = set(model.objects.filter(pk__in=ids).values_list('pk', flat=True))
            by_header = {h: (f, kind) for h, f, kind in _columns(model) if kind != 'label'}

            objs = []
            for row in rows:
                if _as_int(row.get('id')) in taken:
                    continue
                kwargs = {}
                for header, raw in row.items():
                    spec = by_header.get(header)
                    if spec is None:
                        continue            # a label column, or one we do not write
                    f, kind = spec
                    if kind == 'id':
                        kwargs[f.attname] = _to_python(f.target_field, raw)
                    else:
                        kwargs[f.name] = _to_python(f, raw)
                obj = model(**kwargs)
                if model is User:
                    # Password hashes are deliberately kept out of the file, so a
                    # restored account cannot be signed into until an admin sets
                    # a password. Better than shipping credentials in a workbook.
                    obj.set_unusable_password()
                objs.append(obj)
            if objs:
                model.objects.bulk_create(objs, batch_size=500)

    return {'ok': True, 'committed': True, 'plan': plan, 'total': total_new,
            'already_there': total_skip, 'conflicts': [], 'foreign': []}


# ── Reset ────────────────────────────────────────────────────────────────────
def reset_counts(company, modules=None):
    """What a reset of these modules would delete, table by table.

    Counts the cascade too: ticking HR empties part of Sales, and a number that
    hid that would be the one people check before pressing the button.
    """
    doomed = {t.model for m in cascade_modules(modules) for t in TABLES_BY_MODULE[m]}
    return {t.label: t.cls.objects.filter(**{t.scope: company}).count()
            for t in restore_order() if t.model in doomed}


def reset_company(company, modules=None, keep_user_id=None):
    """Delete what these modules own, leaving them at zero.

    Deliberately the exact set of tables the backup covers, walked in reverse
    dependency order — children before the rows they hang off, which is also
    what PROTECT foreign keys (AR accounts guard their bookings) require. Tying
    the two to one registry is what makes "reset, then restore that workbook"
    land back where it started instead of part-way.

    Somebody able to sign in always survives, or the company is unreachable:
    restoring needs a login, and every other account comes back from the
    workbook without a password. Normally that is `keep_user_id`, whoever ran
    the reset. When they belong to a different company — a platform admin
    resetting someone else's — keeping their row would keep nobody here, so
    this company's own admins are kept instead.
    """
    User = apps.get_model('accounts.User')
    keep = set()
    if keep_user_id and User.objects.filter(pk=keep_user_id, company=company).exists():
        keep.add(keep_user_id)
    if not keep:
        keep = set(User.objects.filter(company=company, role='Admin')
                   .values_list('pk', flat=True))
    doomed = {t.model for m in cascade_modules(modules) for t in TABLES_BY_MODULE[m]}
    deleted = {}
    with transaction.atomic():
        for table in reversed(restore_order()):
            if table.model not in doomed:
                continue
            qs = table.cls.objects.filter(**{table.scope: company})
            if table.cls is User and keep:
                qs = qs.exclude(pk__in=keep)
            count = qs.count()
            if count:
                qs.delete()
            deleted[table.label] = count
    return deleted
