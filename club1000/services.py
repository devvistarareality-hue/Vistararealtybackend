import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from dateutil.relativedelta import relativedelta
from .models import Payout, PAYOUT_TYPE_CHOICES

# Every interest instalment (quarterly or monthly) falls on this fixed day of
# its month rather than the month-end — e.g. investing 25 Jun means the first
# quarterly instalment is 10 Jul (a 15-day stub), not 31 Jul.
PAYOUT_DAY = 10


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
    """The PAYOUT_DAY of the first month of the fiscal quarter immediately following `d`'s quarter."""
    idx = _quarter_index(d.month)
    next_idx = (idx + 1) % 4
    target_month = QUARTER_START_MONTHS[next_idx]
    # Only the Q3 (Oct-Dec) -> Q4 (Jan) handoff crosses a calendar year boundary.
    year = d.year + 1 if idx == 2 else d.year
    return date(year, target_month, PAYOUT_DAY)


def default_quarterly_dates(investment_date, tenure_months):
    """Due dates for each quarterly interest instalment — the first one in the
    start month of the fiscal quarter following `investment_date`'s quarter,
    then every 3 months after that. The LAST one is capped at the maturity
    date (investment_date + tenure_months) instead of always landing on the
    next quarter's PAYOUT_DAY — otherwise the stretch from the last regular
    quarterly date to maturity would go completely uncompensated. This means
    the final instalment lands exactly on the maturity date (alongside the
    separate principal payout there) and is usually a stub itself."""
    maturity_date = investment_date + relativedelta(months=tenure_months)
    dates = []
    current = investment_date
    while True:
        nxt = _next_quarter_payout(current)
        if nxt >= maturity_date:
            dates.append(maturity_date)
            break
        dates.append(nxt)
        current = nxt
    return dates


def _next_month_10th(d):
    """The PAYOUT_DAY of the calendar month strictly after `d`'s month."""
    total_month = d.month + 1
    year = d.year + (total_month - 1) // 12
    month = (total_month - 1) % 12 + 1
    return date(year, month, PAYOUT_DAY)


def default_monthly_dates(investment_date, tenure_months):
    """Due dates for each monthly interest instalment, one per calendar
    month on PAYOUT_DAY. The LAST one is capped at the maturity date
    (investment_date + tenure_months) instead of always landing on the next
    month's PAYOUT_DAY — see default_quarterly_dates for why."""
    maturity_date = investment_date + relativedelta(months=tenure_months)
    dates = []
    current = investment_date
    while True:
        nxt = _next_month_10th(current)
        if nxt >= maturity_date:
            dates.append(maturity_date)
            break
        dates.append(nxt)
        current = nxt
    return dates


def maturity_value(principal, total_return_pct, investment_date, maturity_date):
    """Principal + full-tenure interest, day-count basis — the total an
    investor holds by maturity_date regardless of payout cadence (equals the
    sum of every quarterly/monthly instalment, or the one-shot maturity
    payout, whichever cadence is actually chosen — same daily_rate formula as
    generate_payout_schedule, just totalled over the whole tenure instead of
    per instalment)."""
    days = (maturity_date - investment_date).days
    return principal + principal * (total_return_pct / Decimal('100')) * Decimal(days) / Decimal('365')


def generate_payout_schedule(investor, custom_entries=None):
    """Build the Payout ledger for a freshly-created investor.

    Interest payout cadence and return % live on the Investor (prefilled from
    its Scheme at add-time, but editable per-investor there). If the caller
    reviewed/edited the quarterly schedule client-side, `custom_entries`
    (a list of {due_date, amount_due, payout_type}) is used verbatim instead
    of the auto-computed one.

    Auto-computed default:
    - Quarterly / Monthly: one 'interest' row per instalment date (see
      default_quarterly_dates / default_monthly_dates — each due on
      PAYOUT_DAY, except the last which is capped at the maturity date so no
      stretch of the tenure goes uncompensated), sized by actual day-count
      proration at the investor's annual total_return_pct (daily_rate =
      principal * pct/100 / 365, instalment = daily_rate * days since the
      PREVIOUS instalment, or since investment_date for the first one). Both
      the first AND last instalments are usually stubs — e.g. investing 25
      Jun with quarterly payout means a 15-day first instalment due 10 Jul,
      not a full quarter's share — plus one final 'maturity' row for the
      principal only, due the same day as that last (capped) interest row.
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
        daily_rate = principal * (total_return_pct / Decimal('100')) / Decimal('365')
        prev_date = investor.investment_date
        for due_date in dates:
            days_elapsed = (due_date - prev_date).days
            amount_due = (daily_rate * Decimal(days_elapsed)).quantize(Decimal('0.01'))
            Payout.objects.create(
                investor=investor,
                payout_type='interest',
                due_date=due_date,
                amount_due=amount_due,
            )
            prev_date = due_date
        Payout.objects.create(
            investor=investor,
            payout_type='maturity',
            due_date=investor.maturity_date,
            amount_due=principal,
        )
    else:
        total_payable = maturity_value(principal, total_return_pct, investor.investment_date, investor.maturity_date)
        Payout.objects.create(
            investor=investor,
            payout_type='maturity',
            due_date=investor.maturity_date,
            amount_due=total_payable.quantize(Decimal('0.01')),
        )
