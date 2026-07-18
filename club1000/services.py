import re
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from .models import Payout, PAYOUT_TYPE_CHOICES


def normalize_phone(phone):
    """Digits-only, last 10 — collapses +91/spaces/dashes so the same person's
    number always matches regardless of how it was typed."""
    digits = re.sub(r'\D', '', phone or '')
    return digits[-10:] if len(digits) >= 10 else digits

# Company fiscal quarters: Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar.
# Quarterly interest is paid in the FIRST month of the quarter AFTER the one the
# investment falls in — e.g. investing anywhere in Q1 (Apr/May/Jun) pays out in
# July (Q2's start month), Q2 investments pay in October, Q3 in January, Q4 in April.
QUARTER_START_MONTHS = (4, 7, 10, 1)  # Q1, Q2, Q3, Q4 start months (1-indexed)


def _quarter_index(month):
    """0=Q1(Apr-Jun) 1=Q2(Jul-Sep) 2=Q3(Oct-Dec) 3=Q4(Jan-Mar)."""
    return ((month - 4) % 12) // 3


def _next_quarter_payout(d):
    """Last day of the first month of the fiscal quarter immediately following `d`'s quarter."""
    idx = _quarter_index(d.month)
    next_idx = (idx + 1) % 4
    target_month = QUARTER_START_MONTHS[next_idx]
    # Only the Q3 (Oct-Dec) -> Q4 (Jan) handoff crosses a calendar year boundary.
    year = d.year + 1 if idx == 2 else d.year
    return date(year, target_month, monthrange(year, target_month)[1])


def default_quarterly_dates(investment_date, tenure_months):
    """Due dates for each quarterly interest instalment — the first one in the
    start month of the fiscal quarter following `investment_date`'s quarter,
    then every 3 months after that."""
    quarters = max(tenure_months // 3, 1)
    dates = []
    current = investment_date
    for _ in range(quarters):
        current = _next_quarter_payout(current)
        dates.append(current)
    return dates


def _next_month_end(d):
    """Last day of the calendar month strictly after `d`'s month."""
    total_month = d.month + 1
    year = d.year + (total_month - 1) // 12
    month = (total_month - 1) % 12 + 1
    return date(year, month, monthrange(year, month)[1])


def default_monthly_dates(investment_date, tenure_months):
    """Due dates for each monthly interest instalment, one per calendar
    month-end, for the full tenure."""
    dates = []
    current = investment_date
    for _ in range(max(tenure_months, 1)):
        current = _next_month_end(current)
        dates.append(current)
    return dates


def generate_payout_schedule(investor, custom_entries=None):
    """Build the Payout ledger for a freshly-created investor.

    Interest payout cadence and return % live on the Investor (prefilled from
    its Scheme at add-time, but editable per-investor there). If the caller
    reviewed/edited the quarterly schedule client-side, `custom_entries`
    (a list of {due_date, amount_due, payout_type}) is used verbatim instead
    of the auto-computed one.

    Auto-computed default:
    - Quarterly: one 'interest' row every 3 months for the tenure, the
      investor's total_return_pct spread evenly across the quarters, plus one
      final 'maturity' row for the principal only (interest already paid out).
    - Maturity: a single 'maturity' row (principal + full total return) due
      on the maturity date.
    """
    if custom_entries:
        valid_types = {c[0] for c in PAYOUT_TYPE_CHOICES}
        rows = []
        for entry in custom_entries:
            try:
                due_date = datetime.strptime(str(entry.get('due_date', '')), '%Y-%m-%d').date()
                amount_due = Decimal(str(entry.get('amount_due')))
            except (ValueError, TypeError, InvalidOperation):
                continue
            payout_type = entry.get('payout_type')
            if payout_type not in valid_types:
                payout_type = 'interest'
            rows.append(Payout(investor=investor, payout_type=payout_type, due_date=due_date, amount_due=amount_due))
        if rows:
            Payout.objects.bulk_create(rows)
            return

    scheme = investor.scheme
    principal = investor.amount_invested
    total_return_pct = investor.total_return_pct

    if investor.interest_payout in ('quarterly', 'monthly'):
        dates = (
            default_quarterly_dates(investor.investment_date, scheme.tenure_months)
            if investor.interest_payout == 'quarterly'
            else default_monthly_dates(investor.investment_date, scheme.tenure_months)
        )
        interest_total = principal * (total_return_pct / Decimal('100'))
        per_instalment = (interest_total / len(dates)).quantize(Decimal('0.01'))
        for due_date in dates:
            Payout.objects.create(
                investor=investor,
                payout_type='interest',
                due_date=due_date,
                amount_due=per_instalment,
            )
        Payout.objects.create(
            investor=investor,
            payout_type='maturity',
            due_date=investor.maturity_date,
            amount_due=principal,
        )
    else:
        total_payable = principal * (Decimal('1') + total_return_pct / Decimal('100'))
        Payout.objects.create(
            investor=investor,
            payout_type='maturity',
            due_date=investor.maturity_date,
            amount_due=total_payable,
        )
