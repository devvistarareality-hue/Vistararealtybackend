import re
from datetime import date, datetime, timedelta
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


def accrual_end(due_date, maturity_date):
    """The last day an instalment's interest accrues to.

    A payout DATE is when the money is handed over, not the period it covers.
    Investing 1 Jul with quarterly payouts, the first instalment falls due
    10 Oct but pays the quarter that ENDED 30 Sep — accruing all the way to
    the 10th charged ten days that belong to the next quarter.

    So an instalment accrues to the day before its payout month begins. The
    final instalment is the exception: it is capped at the maturity date, and
    maturity is a real end rather than a payment day, so it accrues to itself.

    Periods stay contiguous (each starts where the last ended) and the last
    ends at maturity, so the instalments still sum to exactly the scheme's
    promised total return — this shifts where the boundaries fall, it does not
    change what is paid overall.
    """
    if due_date >= maturity_date:
        return maturity_date
    return date(due_date.year, due_date.month, 1) - timedelta(days=1)


FY_QUARTERS = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def period_label(start, end, cadence):
    """What an interest instalment pays for, by the period it covers (not its
    position in the schedule): quarterly ones by the financial-year quarter —
    Q1 Apr–Jun, Q2 Jul–Sep, Q3 Oct–Dec, Q4 Jan–Mar — monthly ones by the month.
    A period that does not cover the whole quarter / month is marked "part"."""
    if cadence == 'monthly':
        month_start = date(end.year, end.month, 1)
        part = start > month_start or end.month == (end + timedelta(days=1)).month
        return f'{MONTHS[end.month - 1]} {end.year}' + (' (part)' if part else '')
    q = FY_QUARTERS[end.month]
    fy = end.year if end.month >= 4 else end.year - 1
    first_month = {1: 4, 2: 7, 3: 10, 4: 1}[q]
    q_start = date(fy if q < 4 else fy + 1, first_month, 1)
    q_end = (date(q_start.year + (1 if first_month == 10 else 0), (first_month + 2) % 12 + 1, 1) - timedelta(days=1))
    part = start > q_start or end < q_end
    return f'Q{q} FY{str(fy)[2:]}-{str(fy + 1)[2:]}' + (' (part)' if part else '')


def interest_periods(investor, payouts):
    """The working behind each interest instalment of one investor: the period it
    pays for (from → to), its days, the interest per day and the rate used —
    {payout_id: {...}}. Mirrors generate_payout_schedule: periods are contiguous
    from the investment date, each ending where accrual_end puts it, and an
    instalment is days × (principal × total return % ÷ the tenure's days)."""
    if not investor.investment_date or not investor.maturity_date:
        return {}
    tenure_days = (investor.maturity_date - investor.investment_date).days
    principal = investor.amount_invested or Decimal('0')
    pct = investor.total_return_pct or Decimal('0')
    if tenure_days <= 0:
        return {}
    per_day = principal * pct / Decimal('100') / Decimal(tenure_days)
    out = {}
    # An old investment's earlier terms: each instalment against its own term.
    for n, t in enumerate(past_terms(investor), start=1):
        rows = [p for p in payouts if p.payout_type == 'interest' and not p.balance_of_id
                and t['start'] < p.due_date <= t['maturity'] + timedelta(days=31)
                and (p.notes or '').startswith(f'Term {n} of')]
        t_days = (t['maturity'] - t['start']).days or 1
        t_per_day = t['principal'] * t['pct'] / Decimal('100') / Decimal(t_days)
        prev = t['start']
        for p in sorted(rows, key=lambda p: (p.due_date, p.id)):
            end = t['maturity'] if t['at_maturity'] else accrual_end(p.due_date, t['maturity'])
            days = (end - prev).days
            if days <= 0:
                continue
            start = prev + timedelta(days=1)
            out[p.id] = {
                'from': start, 'to': end, 'days': days, 'per_day': t_per_day.quantize(Decimal('0.01')),
                'rate_pct': t['pct'], 'tenure_days': t_days, 'principal': t['principal'],
                'computed': (t_per_day * days).quantize(Decimal('0.01')),
                'label': f'Term {n} · ' + period_label(start, end, t['payout']),
            }
            prev = end
    prev = investor.investment_date
    for p in sorted((p for p in payouts if p.payout_type == 'interest' and not p.balance_of_id), key=lambda p: (p.due_date, p.id)):
        end = accrual_end(p.due_date, investor.maturity_date)
        days = (end - prev).days
        if days <= 0:
            continue
        start = prev + timedelta(days=1)    # interest runs from the day after
        out[p.id] = {
            'from': start, 'to': end, 'days': days,
            'per_day': per_day.quantize(Decimal('0.01')),
            'rate_pct': pct, 'tenure_days': tenure_days, 'principal': principal,
            'computed': (per_day * days).quantize(Decimal('0.01')),
            'label': period_label(start, end, investor.interest_payout),
        }
        prev = end
    # A balance kept due from a short payment pays for the same period.
    parent = {p.id: p.balance_of_id for p in payouts}
    for p in payouts:
        if p.balance_of_id:
            root = p.balance_of_id
            while parent.get(root):
                root = parent[root]
            if root in out:
                out[p.id] = dict(out[root], label=out[root]['label'] + ' · balance')
    return out


def maturity_value(principal, total_return_pct, investment_date, maturity_date):
    """Principal + the scheme's total return, applied once over the whole
    tenure — total_return_pct is the total return BY maturity (e.g. a
    "LEGACY 110" scheme pays 110% total over its 5-year tenure, not 110%
    every year), not an annualized rate to be scaled by day-count. This is
    the total an investor holds by maturity_date regardless of payout
    cadence (equals the sum of every quarterly/monthly instalment, or the
    one-shot maturity payout, whichever cadence is actually chosen — see
    generate_payout_schedule, which spreads this same fixed total interest
    pro-rata across the tenure's actual days)."""
    return principal + principal * (total_return_pct / Decimal('100'))


def term_interest_rows(start, maturity, principal, total_return_pct, payout, tenure_months):
    """The interest instalments of one term — [(due_date, amount)] — exactly as
    generate_payout_schedule sizes them (see its docstring)."""
    dates = (default_quarterly_dates(start, tenure_months) if payout == 'quarterly'
             else default_monthly_dates(start, tenure_months))
    tenure_days = (maturity - start).days
    if tenure_days <= 0:
        return []
    daily_rate = principal * (total_return_pct / Decimal('100')) / Decimal(tenure_days)
    rows, prev_date = [], start
    for due_date in dates:
        # Accrue to the end of the period this instalment pays for, not to
        # the day it is handed over — see accrual_end.
        period_end = accrual_end(due_date, maturity)
        days_elapsed = (period_end - prev_date).days
        if days_elapsed <= 0:
            # Invested on or after the period's own end (e.g. 30 Sep, with
            # the 10 Oct instalment covering up to 30 Sep). There is nothing
            # to pay yet, and a zero row is noise; prev_date deliberately
            # stays put so the next instalment runs from the investment
            # date and no day goes uncounted.
            continue
        rows.append((due_date, (daily_rate * Decimal(days_elapsed)).quantize(Decimal('0.01'))))
        prev_date = period_end
    return rows


def past_terms(investor):
    """The finished terms before the running one (an import of an old investment),
    oldest first, each with what followed it: [{start, maturity, principal, next,
    interest, rolled, returned}]. `next` is the principal the term renewed into;
    at maturity the interest earned pays out, except what was rolled into a larger
    next principal, and a smaller next principal pays the difference back."""
    terms = []
    raw = investor.past_terms or []
    for n, t in enumerate(raw):
        start, maturity = date.fromisoformat(t['start']), date.fromisoformat(t['maturity'])
        principal = Decimal(str(t['principal']))
        if n + 1 < len(raw):
            nxt = Decimal(str(raw[n + 1]['principal']))
        else:
            # The running term's principal as it STARTED, not as it stands now.
            # A premature redemption reduces amount_invested, and reading it
            # raw made the last finished term claim it had renewed into the
            # smaller figure — inventing a "principal paid back to him" line
            # for money that was actually redeemed later, under its own LOI.
            # Adding back what has been redeemed keeps a renewal's history
            # about the renewal.
            nxt = (investor.amount_invested or Decimal('0')) + (investor.redeemed_principal or Decimal('0'))
        pct = Decimal(str(t.get('return_pct', investor.total_return_pct) or 0))
        at_maturity = (t.get('interest_payout') or investor.interest_payout) not in ('quarterly', 'monthly')
        interest = (maturity_value(principal, pct, start, maturity) - principal) if at_maturity else (principal * pct / Decimal('100'))
        if t.get('rolled_interest') not in (None, ''):
            rolled = Decimal(str(t['rolled_interest']))          # recorded at the renewal
        else:
            rolled = max(Decimal('0'), min(interest, nxt - principal)) if at_maturity else Decimal('0')
        terms.append({'start': start, 'maturity': maturity, 'principal': principal, 'next': nxt, 'pct': pct,
                      'interest': interest.quantize(Decimal('0.01')), 'rolled': rolled.quantize(Decimal('0.01')),
                      'returned': max(Decimal('0'), principal - nxt).quantize(Decimal('0.01')),
                      'at_maturity': at_maturity, 'payout': t.get('interest_payout') or investor.interest_payout})
    return terms


def money_in(investor):
    """Everything the investor actually put in: the first term's principal plus any
    top-up a renewal added beyond what the matured term handed back. Without past
    terms it is simply the amount invested."""
    terms = past_terms(investor)
    if not terms:
        # Money taken out by a part-redemption was still put in.
        return (investor.amount_invested or Decimal('0')) + redeemed_between(investor, investor.investment_date, None)
    total = terms[0]['principal']
    for t in terms:
        # New money at a renewal = what it renewed with beyond the principal and the
        # interest that was added into it.
        total += max(Decimal('0'), t['next'] - t['principal'] - t['rolled'])
    return total


def redeemed_between(investor, start, end):
    """Principal taken out by part-redemptions dated from `start` to before `end` (None =
    to date). amount_invested is what is STILL invested, so a term's principal — what
    it started with — is that plus what was redeemed during it. Only the partial-aware
    path records redemption_detail; the old all-or-nothing redeem leaves the amount."""
    total = Decimal('0')
    for p in investor.payouts.all():
        d = p.redemption_detail
        if p.payout_type == 'premature_redemption' and d and p.due_date and (not start or p.due_date >= start) \
                and (end is None or p.due_date < end):
            total += Decimal(str(d.get('redeem_amount') or 0))
    return total


def term_list(investor):
    """Every term of an investment, oldest first, the running one last:
    [{no, start, maturity, principal, label}] — no 0 is the original investment."""
    out = []
    # A term's principal is what it STARTED with: what is left plus what was redeemed in it.
    for n, t in enumerate(investor.past_terms or []):
        start, maturity = date.fromisoformat(t['start']), date.fromisoformat(t['maturity'])
        out.append({'no': n, 'start': start, 'maturity': maturity, 'current': False,
                    'principal': Decimal(str(t['principal'])) + redeemed_between(investor, start, maturity)})
    if investor.investment_date and investor.maturity_date:
        out.append({'no': len(out), 'start': investor.investment_date, 'maturity': investor.maturity_date,
                    'principal': (investor.amount_invested or Decimal('0'))
                    + redeemed_between(investor, investor.investment_date, None), 'current': True})
    # Renewals made before terms were kept left no record of the earlier ones: count
    # from the renewal number so the running term still reads 'Renewal N'.
    offset = max(0, (investor.renewal_count or 0) - len(investor.past_terms or []))
    for t in out:
        span = f'{t["start"]:%Y}–{t["maturity"]:%y}' if t['start'].year != t['maturity'].year else f'{t["start"]:%Y}'
        t['renewal_no'] = t['no'] + offset
        name = 'Original' if t['renewal_no'] == 0 else 'Renewal %d' % t['renewal_no']
        t['label'] = f'{name} · {span}'
    return out


def term_of(terms, due_date):
    """Which term a payout belongs to: the last one that had started before it fell due
    (a term's own maturity payout falls on the day the next one starts)."""
    pick = 0
    for t in terms:
        if due_date and t['start'] < due_date:
            pick = t['no']
    return pick


def close_term(investor, term, next_principal, maturity_paid=False, roll_room=None):
    """On a renewal: the term just finished, as its payouts should stand — its
    at-maturity interest (less whatever was rolled into a larger renewal) and the
    principal paid back when it renewed for less. Quarterly / monthly interest
    instalments of that term are already on the books."""
    principal, pct = Decimal(str(term['principal'])), Decimal(str(term.get('return_pct') or 0))
    start, maturity = date.fromisoformat(term['start']), date.fromisoformat(term['maturity'])
    at_maturity = (term.get('interest_payout') or 'maturity') not in ('quarterly', 'monthly')
    rows = []
    label = f'Term ending {maturity:%d-%m-%Y}'
    rolled = Decimal('0')
    if at_maturity and not maturity_paid:
        interest = maturity_value(principal, pct, start, maturity) - principal
        room = max(Decimal('0'), next_principal - principal) if roll_room is None else roll_room
        rolled = max(Decimal('0'), min(interest, room))
        if interest - rolled > 0:
            rows.append(Payout(investor=investor, payout_type='interest', due_date=maturity,
                               amount_due=(interest - rolled).quantize(Decimal('0.01')),
                               notes=label + (f' · ₹{rolled:,.0f} of the interest renewed' if rolled else '')))
    returned = (principal - next_principal).quantize(Decimal('0.01'))
    if returned > 0 and not maturity_paid:
        rows.append(Payout(investor=investor, payout_type='maturity', due_date=maturity, amount_due=returned,
                           notes=label + ' · principal returned on renewal'))
    Payout.objects.bulk_create(rows)
    return rolled


def generate_past_term_payouts(investor):
    """Payouts for the finished terms of an imported old investment — what an
    in-app renewal leaves behind: every interest instalment of each term, and
    on its maturity the principal paid back when the next term was smaller.
    They are created due (pending); the payments made are entered in Payouts."""
    rows = []
    terms = past_terms(investor)
    for n, t in enumerate(terms, start=1):
        label = f'Term {n} of {len(terms) + 1} · {t["start"]:%d-%m-%Y} → {t["maturity"]:%d-%m-%Y}'
        if t['at_maturity']:
            paid_out = (t['interest'] - t['rolled']).quantize(Decimal('0.01'))
            if paid_out > 0:
                rows.append(Payout(investor=investor, payout_type='interest', due_date=t['maturity'], amount_due=paid_out,
                                   notes=label + (f' · ₹{t["rolled"]:,.0f} of the interest renewed' if t['rolled'] else '')))
        else:
            for due, amt in term_interest_rows(t['start'], t['maturity'], t['principal'], t['pct'], t['payout'],
                                               investor.scheme.tenure_months):
                rows.append(Payout(investor=investor, payout_type='interest', due_date=due, amount_due=amt, notes=label))
        if t['returned'] > 0:
            rows.append(Payout(investor=investor, payout_type='maturity', due_date=t['maturity'], amount_due=t['returned'],
                               notes=label + ' · principal returned on renewal'))
    Payout.objects.bulk_create(rows)
    return rows


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
      proration of the investor's FIXED total return over the tenure
      (total_return_pct is the total return by maturity, not an annualized
      rate — see maturity_value): daily_rate = (principal * pct/100) /
      tenure_days, instalment = daily_rate * days since the PREVIOUS
      instalment, or since investment_date for the first one. Both the first
      AND last instalments are usually stubs — e.g. investing 25 Jun with
      quarterly payout means a 15-day first instalment due 10 Jul, not a full
      quarter's share — plus one final 'maturity' row for the principal
      only, due the same day as that last (capped) interest row. The sum of
      every instalment therefore always equals principal * pct/100, exactly
      matching maturity_value's total interest.
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
            # The client previews a zero for an instalment whose period had not
            # started at the investment date (see accrual_end); the auto path
            # omits that row, so a reviewed schedule must not create it either.
            if payout_type == 'interest' and amount_due <= 0:
                continue
            rows.append(Payout(investor=investor, payout_type=payout_type, due_date=due_date, amount_due=amount_due))
        if rows:
            Payout.objects.bulk_create(rows)
            return

    scheme = investor.scheme
    principal = investor.amount_invested
    total_return_pct = investor.total_return_pct

    if investor.interest_payout in ('quarterly', 'monthly'):
        for due_date, amount_due in term_interest_rows(investor.investment_date, investor.maturity_date, principal,
                                                       total_return_pct, investor.interest_payout, scheme.tenure_months):
            Payout.objects.create(
                investor=investor,
                payout_type='interest',
                due_date=due_date,
                amount_due=amount_due,
            )
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


def _short_term(investor):
    """'Original' or 'Renewal N' — the running term's name."""
    n = investor.renewal_count or 0
    return 'Original' if n == 0 else f'Renewal {n}'


def legacy_lois(investor):
    """The LOIs still stored for a record from before loi_history was kept: each kept
    term's LOI, each redemption's LOI (on its payout), and the current one."""
    out, seen = [], set()

    def add(kind, label, path, at):
        if path and path not in seen:
            seen.add(path)
            out.append({'kind': kind, 'label': label, 'path': path, 'at': str(at or ''), 'by': ''})
    terms = term_list(investor)
    for t, pt in zip(terms, investor.past_terms or []):
        name = t['label'].split(' · ')[0]
        add('renewal' if t['renewal_no'] else 'new', f'{name} LOI', pt.get('loi_document'), t['start'])
    pays = sorted((p for p in investor.payouts.all() if p.payout_type == 'premature_redemption' and p.loi_document),
                  key=lambda p: (p.due_date, p.id))
    for n, p in enumerate(pays, start=1):
        add('redemption', f'Redemption {n} LOI', p.loi_document.name if hasattr(p.loi_document, 'name') else p.loi_document, p.due_date)
    if investor.loi_document:
        at = investor.approved_at.date() if investor.approved_at else investor.investment_date
        add('current', f'{_short_term(investor)} LOI', investor.loi_document.name, at)
    return sorted(out, key=lambda e: e['at'])


def investment_lois(investor):
    """The LOI register as the Ledger lists it: stored history, or rebuilt for older
    records. Each carries `i`, its place in that list (used to open it)."""
    rows = investor.loi_history if investor.loi_history is not None else legacy_lois(investor)
    return [{**r, 'i': i} for i, r in enumerate(rows) if r.get('path')]
