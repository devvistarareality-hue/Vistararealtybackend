"""Glue between bookings, AR accounts and the engine."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from sales.models import Booking
from .engine import PlanLine, Receipt, compute
from .models import ARAccount

ZERO = Decimal('0')


def _d(x) -> Decimal:
    try:
        return Decimal(str(x)) if x not in (None, '') else ZERO
    except (InvalidOperation, ValueError):
        return ZERO


def parse_date(s):
    """Installment dates are stored YYYY-MM-DD; tolerate the display formats too."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%d/%m/%y', '%d-%m-%y'):
        try:
            return datetime.strptime(str(s).strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def is_fully_approved(b: Booking) -> bool:
    """AR starts only once BOTH Sales (status='sold') and Accounts have approved."""
    return b.status == 'sold' and (b.accounts_status or 'approved') == 'approved' and not b.cancelled_at


def legal_other_amount(b: Booking) -> Decimal:
    """"Legal & Other Charges" as the company collects them: total_extra less stamp
    duty and registration, which the buyer pays straight to the government. This
    is why the workbook's Total Collectable = Total Deal − Stamp Duty − Reg."""
    return max(ZERO, _d(b.total_extra) - _d(b.stamp_duty) - _d(b.reg_fees))


def build_plan(b: Booking, legal_due=None):
    lines, seen = [], set()

    def add(inst, kind, prefix):
        amt = _d(inst.get('amt'))
        if amt <= 0:
            return
        no = str(inst.get('no') or len(lines) + 1)
        key = f'{prefix}{no}' if prefix else no
        if key in seen:
            return
        seen.add(key)
        label = {'inst': f'Installment {no}', 'nsd': f'NSD installment {no}', 'extra': f'Extra work {no}'}[kind]
        lines.append(PlanLine(key, (prefix.upper().rstrip('-') + no) if prefix else no, label,
                              parse_date(inst.get('date')), amt, kind))

    # Most LOIs carry the extra charges as their own installment, no="Extra", for
    # the whole of total_extra — stamp duty and registration included. That line
    # IS "Legal & Other Charges": it is collected less stamp and reg (paid to the
    # government directly), and it must not be added a second time.
    extra_inst = None
    for inst in (b.installments or []):
        if str(inst.get('no') or '').strip().lower() == 'extra':
            extra_inst = inst
        elif inst.get('isExtraWork'):
            add(inst, 'extra', 'e-')
        elif inst.get('isNsd'):
            add(inst, 'nsd', 'n-')
        else:
            add(inst, 'inst', '')
    for inst in (b.extra_work_inst or []):
        add(inst, 'extra', 'e-')

    if extra_inst is not None:
        legal = max(ZERO, _d(extra_inst.get('amt')) - _d(b.stamp_duty) - _d(b.reg_fees))
        # The LOI's date applies unless Accounts set one (sale deed / possession).
        due = legal_due or parse_date(extra_inst.get('date'))
    else:
        legal = legal_other_amount(b)
        due = legal_due
    if legal > 0:
        lines.append(PlanLine('legal', 'L', 'Legal & Other Charges', due, legal, 'legal'))
    return lines


def expected_collectable(b: Booking) -> Decimal:
    return _d(b.final_amount) - _d(b.stamp_duty) - _d(b.reg_fees)


# Only plain columns: loading a Booking normally decrypts ~30 encrypted fields,
# and the sync looks at every sold booking on every register load.
_SYNC_FIELDS = ('id', 'company_id', 'status', 'accounts_status', 'cancelled_at', 'revision_of_id', 'revision_no')


def _root_id(bid, parent):
    seen = set()
    while parent.get(bid) and bid not in seen:
        seen.add(bid)
        bid = parent[bid]
    return bid


@transaction.atomic
def sync_accounts(bookings_qs):
    """Make sure every fully-approved booking has an AR account pointing at the
    latest approved booking of its revision chain, and freeze accounts whose deal
    was cancelled. Idempotent and cheap — a few queries, no decryption, no
    per-row inserts — because it runs on every register load."""
    rows = list(bookings_qs.values(*_SYNC_FIELDS))
    # Revision chains can reach outside the queryset (an older root that was
    # later cancelled/revised), so resolve parents for everything referenced.
    parent = {r['id']: r['revision_of_id'] for r in rows}
    missing = {r['revision_of_id'] for r in rows if r['revision_of_id'] and r['revision_of_id'] not in parent}
    while missing:
        more = list(Booking.objects.filter(id__in=missing).values('id', 'revision_of_id'))
        for m in more:
            parent[m['id']] = m['revision_of_id']
        missing = {m['revision_of_id'] for m in more if m['revision_of_id'] and m['revision_of_id'] not in parent}

    latest = {}   # root id → (company_id, latest approved booking id, (revision_no, id))
    for r in rows:
        if not (r['status'] == 'sold' and (r['accounts_status'] or 'approved') == 'approved' and not r['cancelled_at']):
            continue
        root = _root_id(r['id'], parent)
        rank = (r['revision_no'] or 0, r['id'])
        if root not in latest or rank > latest[root][2]:
            latest[root] = (r['company_id'], r['id'], rank)

    existing = {a.root_booking_id: a for a in ARAccount.objects.filter(root_booking_id__in=list(latest)).only(
        'id', 'root_booking_id', 'booking_id', 'status')}
    new_accounts, repoint = [], []
    for root, (company_id, bid, _) in latest.items():
        acct = existing.get(root)
        if acct is None:
            new_accounts.append(ARAccount(company_id=company_id, root_booking_id=root, booking_id=bid))
        elif acct.booking_id != bid and acct.status == 'active':
            # A revision was approved: the plan follows it, receipts stay put.
            acct.booking_id = bid
            repoint.append(acct)
    if new_accounts:
        ARAccount.objects.bulk_create(new_accounts, ignore_conflicts=True)
    if repoint:
        ARAccount.objects.bulk_update(repoint, ['booking'])

    # Cancelled deals: freeze, keeping receipts and history.
    ARAccount.objects.filter(status='active', booking__cancelled_at__isnull=False).update(
        status='frozen', frozen_at=timezone.now())


def compute_account(acct: ARAccount, as_of=None, receipts=None):
    b = acct.booking
    as_of = as_of or timezone.localdate()
    if receipts is None:
        receipts = [r for r in acct.receipts.all() if not r.is_deleted]
    plan = build_plan(b, acct.legal_due_date)
    result = compute(plan, [Receipt(r.id, r.paid_on, _d(r.amount), r.mode, r.remarks or '') for r in receipts], as_of)
    mismatch = result.collectable - expected_collectable(b)
    return plan, result, mismatch
