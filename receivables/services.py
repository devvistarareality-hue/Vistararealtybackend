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

    for inst in (b.installments or []):
        if inst.get('isExtraWork'):
            add(inst, 'extra', 'e-')
        elif inst.get('isNsd'):
            add(inst, 'nsd', 'n-')
        else:
            add(inst, 'inst', '')
    for inst in (b.extra_work_inst or []):
        add(inst, 'extra', 'e-')

    legal = legal_other_amount(b)
    if legal > 0:
        lines.append(PlanLine('legal', 'L', 'Legal & Other Charges', legal_due, legal, 'legal'))
    return lines


def expected_collectable(b: Booking) -> Decimal:
    return _d(b.final_amount) - _d(b.stamp_duty) - _d(b.reg_fees)


def root_of(b: Booking) -> Booking:
    seen = set()
    while b.revision_of_id and b.revision_of_id not in seen:
        seen.add(b.id)
        b = b.revision_of
    return b


@transaction.atomic
def sync_accounts(bookings_qs):
    """Make sure every fully-approved booking has an AR account pointing at the
    latest approved booking of its revision chain, and freeze accounts whose deal
    was cancelled. Idempotent — safe to call on every register load."""
    approved = [b for b in bookings_qs.select_related('revision_of') if is_fully_approved(b)]
    latest = {}
    for b in approved:
        root = root_of(b)
        cur = latest.get(root.id)
        if cur is None or (b.revision_no or 0, b.id) > (cur[1].revision_no or 0, cur[1].id):
            latest[root.id] = (root, b)

    existing = {a.root_booking_id: a for a in ARAccount.objects.filter(root_booking_id__in=list(latest))}
    for root_id, (root, b) in latest.items():
        acct = existing.get(root_id)
        if acct is None:
            ARAccount.objects.create(company_id=b.company_id, root_booking=root, booking=b)
        elif acct.booking_id != b.id and acct.status == 'active':
            # A revision was approved: the plan follows it, receipts stay put.
            acct.booking = b
            acct.save(update_fields=['booking', 'updated_at'])

    # Cancelled deals: freeze, keeping receipts and history.
    for acct in ARAccount.objects.filter(status='active', booking__cancelled_at__isnull=False):
        acct.status = 'frozen'
        acct.frozen_at = timezone.now()
        acct.save(update_fields=['status', 'frozen_at', 'updated_at'])


def compute_account(acct: ARAccount, as_of=None, receipts=None):
    b = acct.booking
    as_of = as_of or timezone.localdate()
    if receipts is None:
        receipts = [r for r in acct.receipts.all() if not r.is_deleted]
    plan = build_plan(b, acct.legal_due_date)
    result = compute(plan, [Receipt(r.id, r.paid_on, _d(r.amount), r.mode, r.remarks or '') for r in receipts], as_of)
    mismatch = result.collectable - expected_collectable(b)
    return plan, result, mismatch
