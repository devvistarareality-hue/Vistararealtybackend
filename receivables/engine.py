"""AR calculation engine — pure functions, no database.

Reproduces the company's Excel AR workbook (Payment Plan / Payment Tracker /
Plot Master macros) exactly:

* Receipts are applied oldest-first (FIFO) to installments in due-date order.
  A receipt larger than what is left on an installment spills into the next one
  — that spill is the workbook's Overpaid → Carry-In chain.
* Interest, per slice of money allocated to an installment:
    - paid before the due date:      slice × 1% × days early ÷ 30, as a CREDIT
    - paid 0–10 days after due date: no interest (grace)
    - paid more than 10 days late:   slice × 2% × days late ÷ 30 — every day
      counts, not only the days beyond grace
* An installment still unpaid past its due date accrues the same 2% (after the
  same 10-day grace) up to the as-of date.
* Money received beyond the whole plan (overpaid) earns the 1% credit from the
  day it was paid to the as-of date.
* A line with no due date (Legal & Other Charges before a date is set) absorbs
  money like any other line but carries no interest either way.

Receipts only ever pay installments; interest accrues alongside, and what the
customer really owes is "O/s with interest".

Verified against the workbook's own Plot 10 (Kalrav 2) — see tests.py.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

LATE_RATE = Decimal('0.02')     # per month, on late money
EARLY_RATE = Decimal('0.01')    # per month, credit on early / overpaid money
GRACE_DAYS = 10
DAYS_PER_MONTH = Decimal('30')

AGEING_BUCKETS = [  # (label, lower-inclusive, upper-inclusive) days past due
    ('0-15', 0, 15), ('16-30', 16, 30), ('31-60', 31, 60), ('61-90', 61, 90),
    ('91-120', 91, 120), ('121-180', 121, 180), ('>180', 181, None),
]

ZERO = Decimal('0')


def rupees(x: Decimal) -> int:
    """Round half-up to whole rupees, as the workbook displays."""
    return int(Decimal(x).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


@dataclass
class PlanLine:
    key: str                 # stable id, e.g. "1", "nsd-2", "extra-1", "legal"
    no: str                  # what the ledger prints as "Inst No"
    label: str
    due: Optional[date]
    amount: Decimal
    kind: str = 'inst'       # inst | nsd | extra | legal


@dataclass
class Receipt:
    id: object
    paid_on: date
    amount: Decimal
    mode: str = ''
    remarks: str = ''


@dataclass
class InterestRow:
    line_key: str
    inst_no: str
    due: Optional[date]
    paid_on: Optional[date]  # None = still unpaid (accruing to as-of)
    amount: Decimal
    days: Optional[int]
    interest: Decimal
    receipt_id: object = None


@dataclass
class LineState:
    line: PlanLine
    paid: Decimal = ZERO
    status: str = 'pending'  # completed | partial | pending
    remaining: Decimal = ZERO


@dataclass
class Result:
    lines: List[LineState]
    rows: List[InterestRow]
    collectable: Decimal
    received: Decimal
    outstanding: Decimal          # collectable − received (negative = overpaid)
    overdue: Decimal              # unpaid on lines due on/before as-of
    not_due: Decimal              # unpaid on lines not yet due, or undated
    overpaid: Decimal
    overpaid_credit: Decimal      # ≤ 0
    net_interest: Decimal         # installment interest + overpaid credit
    os_with_interest: Decimal
    ageing: dict = field(default_factory=dict)       # bucket label → overdue amount
    month_forecast: list = field(default_factory=list)  # [(label, amount)]


def _slice_interest(amount: Decimal, days: int) -> Decimal:
    if days < 0:
        return amount * EARLY_RATE * Decimal(days) / DAYS_PER_MONTH   # negative = credit
    if days <= GRACE_DAYS:
        return ZERO
    return amount * LATE_RATE * Decimal(days) / DAYS_PER_MONTH


def _bucket(days: int) -> str:
    for label, lo, hi in AGEING_BUCKETS:
        if days >= lo and (hi is None or days <= hi):
            return label
    return AGEING_BUCKETS[0][0]


def _month_label(d: date) -> str:
    return d.strftime('%b-%y')


def compute(plan: List[PlanLine], receipts: List[Receipt], as_of: date, forecast_months: int = 3) -> Result:
    # Dated lines in due order; undated lines last, in their given order. The sort
    # is stable, so equal due dates keep plan order (installment 3 before 4).
    ordered = sorted(
        [l for l in plan if l.amount > 0],
        key=lambda l: (l.due is None, l.due or date.max),
    )
    pays = sorted([r for r in receipts if r.amount and r.amount > 0], key=lambda r: (r.paid_on, str(r.id)))
    left = [Decimal(r.amount) for r in pays]

    states = [LineState(line=l, remaining=Decimal(l.amount)) for l in ordered]
    rows: List[InterestRow] = []
    ai = 0

    for st in states:
        ln = st.line
        while st.remaining > 0 and ai < len(pays):
            if left[ai] <= 0:
                ai += 1
                continue
            alloc = min(left[ai], st.remaining)
            if ln.due is not None:
                days = (pays[ai].paid_on - ln.due).days
                intr = _slice_interest(alloc, days)
            else:
                days, intr = None, ZERO
            rows.append(InterestRow(ln.key, ln.no, ln.due, pays[ai].paid_on, alloc, days, intr, pays[ai].id))
            left[ai] -= alloc
            st.remaining -= alloc
            st.paid += alloc
            if left[ai] <= 0:
                ai += 1

        # What is still unpaid on a line already due keeps accruing to as-of.
        if st.remaining > 0 and ln.due is not None and ln.due <= as_of:
            days = (as_of - ln.due).days
            intr = ZERO if days <= GRACE_DAYS else st.remaining * LATE_RATE * Decimal(days) / DAYS_PER_MONTH
            rows.append(InterestRow(ln.key, ln.no, ln.due, None, st.remaining, days, intr))

        st.status = 'completed' if st.remaining <= 0 else ('partial' if st.paid > 0 else 'pending')

    # Money beyond the whole plan earns the early-payment credit up to as-of.
    overpaid = ZERO
    overpaid_credit = ZERO
    for i, rem in enumerate(left):
        if rem > 0:
            overpaid += rem
            held = (as_of - pays[i].paid_on).days
            if held > 0:
                overpaid_credit -= rem * EARLY_RATE * Decimal(held) / DAYS_PER_MONTH

    collectable = sum((Decimal(l.amount) for l in ordered), ZERO)
    received = sum((Decimal(r.amount) for r in pays), ZERO)
    outstanding = collectable - received

    overdue = ZERO
    not_due = ZERO
    ageing = {label: ZERO for label, _, _ in AGEING_BUCKETS}
    forecast = {}
    for st in states:
        if st.remaining <= 0:
            continue
        due = st.line.due
        if due is not None and due <= as_of:
            overdue += st.remaining
            ageing[_bucket((as_of - due).days)] += st.remaining
        else:
            not_due += st.remaining
            forecast.setdefault(_month_label(due) if due else 'No date', ZERO)
            forecast[_month_label(due) if due else 'No date'] += st.remaining

    # Forecast in the workbook's shape: this month, the next N-1, then "After", then undated.
    months = []
    y, m = as_of.year, as_of.month
    for _ in range(forecast_months):
        months.append(date(y, m, 1))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    labels = [_month_label(d) for d in months]
    after = ZERO
    for st in states:
        due = st.line.due
        if st.remaining > 0 and due is not None and due > as_of and date(due.year, due.month, 1) > months[-1]:
            after += st.remaining
    month_forecast = [(lbl, forecast.get(lbl, ZERO)) for lbl in labels]
    month_forecast.append((f'After {labels[-1]}', after))
    if 'No date' in forecast:
        month_forecast.append(('No date', forecast['No date']))

    installment_interest = sum((r.interest for r in rows), ZERO)
    net_interest = installment_interest + overpaid_credit

    return Result(
        lines=states, rows=rows, collectable=collectable, received=received,
        outstanding=outstanding, overdue=overdue, not_due=not_due,
        overpaid=overpaid, overpaid_credit=overpaid_credit,
        net_interest=net_interest, os_with_interest=outstanding + net_interest,
        ageing=ageing, month_forecast=month_forecast,
    )
