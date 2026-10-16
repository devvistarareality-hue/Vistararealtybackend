"""An instalment pays the period it covers, not the day it is handed over.

Quarterly interest falls due on the 10th of the month after the quarter ends:
invest on 1 Jul and the first instalment is due 10 Oct. It accrued all the way
to the 10th, so it charged 101 days — the 91 days of the quarter it pays for,
plus ten days that belong to the next one. Every instalment was shifted the
same way.

It now accrues to the day before its payout month begins (30 Sep for a 10 Oct
instalment), except the last, which is capped at maturity and accrues to
itself. Periods stay contiguous and end at maturity, so the instalments still
sum to the scheme's promised return — this moves where the boundaries fall, it
does not change what is paid overall, and the tests below pin that.

(Pre-existing and unrelated: each instalment is rounded to the paisa as it is
written, so over many instalments the sum can sit a few paise either side of
the promised figure. See _assert_total.)
"""
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase

from accounts.models import User
from club1000.models import Investor, Payout, Scheme
from club1000.services import accrual_end, generate_payout_schedule
from companies.models import Company


class PayoutAccrualTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='PAC', name='Accrual Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@pac.com', company=cls.co, user_code='PAC001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])

    def _scheme(self, months=12, payout='quarterly'):
        return Scheme.objects.create(
            company=self.co, name=f'S{months}{payout}', tenure_months=months,
            min_ticket_size=1, interest_payout_options=[payout])

    def _investor(self, invested, months=12, payout='quarterly',
                  amount='1000000', pct='15'):
        scheme = self._scheme(months, payout)
        return Investor.objects.create(
            company=self.co, scheme=scheme, added_by=self.admin,
            name='An Investor', phone='+919800000001',
            amount_invested=Decimal(amount), total_return_pct=Decimal(pct),
            investment_date=invested,
            maturity_date=invested + relativedelta(months=months),
            interest_payout=payout, status='active', approval_status='approved')

    def _schedule(self, inv):
        generate_payout_schedule(inv)
        return list(Payout.objects.filter(investor=inv, payout_type='interest')
                    .order_by('due_date').values_list('due_date', 'amount_due'))

    # ------------------------------------------------------- the reported case

    def test_the_first_quarterly_instalment_stops_at_the_quarter_end(self):
        """Invested 1 Jul, first payout 10 Oct: it pays 1 Jul–30 Sep, which is
        91 days, not the 101 it used to charge by running to the 10th."""
        inv = self._investor(date(2026, 7, 1))
        rows = self._schedule(inv)
        due, amount = rows[0]
        self.assertEqual(due, date(2026, 10, 10))
        daily = Decimal('1000000') * Decimal('15') / 100 / Decimal(365)
        self.assertEqual(amount, (daily * 91).quantize(Decimal('0.01')))
        # The ten days that are no longer in it.
        self.assertNotEqual(amount, (daily * 101).quantize(Decimal('0.01')))

    def test_the_whole_schedule_for_that_investor(self):
        inv = self._investor(date(2026, 7, 1))
        rows = self._schedule(inv)
        self.assertEqual([d for d, _ in rows],
                         [date(2026, 10, 10), date(2027, 1, 10),
                          date(2027, 4, 10), date(2027, 7, 1)])
        daily = Decimal('1000000') * Decimal('15') / 100 / Decimal(365)
        self.assertEqual([a for _, a in rows],
                         [(daily * n).quantize(Decimal('0.01')) for n in (91, 92, 90, 92)])

    # ----------------------------------------------- the invariant that must hold

    def test_the_instalments_still_add_up_to_the_promised_return(self):
        """The point most worth protecting: shortening the first period must not
        quietly reduce what the investor is owed — the last instalment runs to
        maturity, so the total is unchanged."""
        inv = self._investor(date(2026, 7, 1))
        rows = self._schedule(inv)
        self.assertEqual(sum(a for _, a in rows), Decimal('150000.00'))

    def _assert_total(self, rows, expected):
        """Each instalment is rounded to the paisa as it is written, so over n
        instalments the sum can sit up to n/2 paise either side of the promised
        figure. That drift is pre-existing and has nothing to do with the
        accrual window; what matters here is that shortening the first period
        did not quietly reduce the total."""
        total = sum(a for _, a in rows)
        tolerance = Decimal('0.01') * len(rows)
        self.assertLessEqual(abs(total - expected), tolerance,
                             f'{total} is not within rounding of {expected}')

    def test_that_holds_for_monthly_too(self):
        inv = self._investor(date(2026, 7, 1), payout='monthly')
        self._assert_total(self._schedule(inv), Decimal('150000.00'))

    def test_that_holds_for_an_awkward_start_date(self):
        inv = self._investor(date(2026, 6, 25), months=18, amount='750000', pct='22.5')
        expected = (Decimal('750000') * Decimal('22.5') / 100).quantize(Decimal('0.01'))
        self._assert_total(self._schedule(inv), expected)

    def test_no_day_of_the_tenure_goes_uncounted(self):
        inv = self._investor(date(2026, 6, 25), months=18)
        generate_payout_schedule(inv)
        ends = [accrual_end(d, inv.maturity_date) for d in
                Payout.objects.filter(investor=inv, payout_type='interest')
                .order_by('due_date').values_list('due_date', flat=True)]
        prev, total = inv.investment_date, 0
        for e in ends:
            total += (e - prev).days
            prev = e
        self.assertEqual(total, (inv.maturity_date - inv.investment_date).days)
        self.assertEqual(prev, inv.maturity_date, 'the last period must end at maturity')

    # ------------------------------------------------------------ monthly shape

    def test_a_monthly_instalment_pays_the_month_that_ended(self):
        # Due 10 Aug pays 1 Jul-31 Jul, not 1 Jul-10 Aug.
        inv = self._investor(date(2026, 7, 1), payout='monthly')
        due, amount = self._schedule(inv)[0]
        self.assertEqual(due, date(2026, 8, 10))
        daily = Decimal('1000000') * Decimal('15') / 100 / Decimal(365)
        # 1 Jul to 31 Jul is 30 days between the two dates.
        self.assertEqual(amount, (daily * 30).quantize(Decimal('0.01')))

    # ------------------------------------------------------------- the edges

    def test_the_last_instalment_accrues_to_maturity_itself(self):
        """Maturity is a real end, not a payment day — capping it at the start
        of its own month would leave the final stretch unpaid."""
        inv = self._investor(date(2026, 7, 1))
        self.assertEqual(accrual_end(date(2027, 7, 1), date(2027, 7, 1)), date(2027, 7, 1))

    def test_investing_on_a_period_end_creates_no_empty_instalment(self):
        """Invest 30 Sep and the 10 Oct instalment covers up to 30 Sep — zero
        days. A zero-rupee row is noise, and the days are not lost: the next
        instalment runs from the investment date."""
        inv = self._investor(date(2026, 9, 30))
        rows = self._schedule(inv)
        self.assertNotIn(Decimal('0.00'), [a for _, a in rows])
        self.assertNotIn(date(2026, 10, 10), [d for d, _ in rows])
        self._assert_total(rows, (Decimal('1000000') * Decimal('15') / 100).quantize(Decimal('0.01')))

    def test_accrual_end_is_the_day_before_the_payout_month(self):
        far = date(2099, 1, 1)
        self.assertEqual(accrual_end(date(2026, 10, 10), far), date(2026, 9, 30))
        self.assertEqual(accrual_end(date(2027, 1, 10), far), date(2026, 12, 31))
        self.assertEqual(accrual_end(date(2027, 4, 10), far), date(2027, 3, 31))
        # A leap February, to be sure the month-end is read rather than assumed.
        self.assertEqual(accrual_end(date(2028, 3, 10), far), date(2028, 2, 29))

    # ------------------------------------------------- a reviewed schedule

    def test_a_reviewed_schedule_drops_a_zero_instalment(self):
        """The client previews a zero for a period that had not started; the
        auto path omits that row, so a reviewed one must not create it."""
        inv = self._investor(date(2026, 7, 1))
        generate_payout_schedule(inv, custom_entries=[
            {'due_date': '2026-10-10', 'amount_due': '0', 'payout_type': 'interest'},
            {'due_date': '2027-01-10', 'amount_due': '37808.22', 'payout_type': 'interest'},
        ])
        rows = list(Payout.objects.filter(investor=inv).values_list('due_date', 'amount_due'))
        self.assertEqual(rows, [(date(2027, 1, 10), Decimal('37808.22'))])

    def test_a_reviewed_schedule_is_otherwise_used_verbatim(self):
        inv = self._investor(date(2026, 7, 1))
        generate_payout_schedule(inv, custom_entries=[
            {'due_date': '2026-10-10', 'amount_due': '111.11', 'payout_type': 'interest'},
        ])
        self.assertEqual(
            list(Payout.objects.filter(investor=inv).values_list('amount_due', flat=True)),
            [Decimal('111.11')])

    # --------------------------------------------------------- the other cadence

    def test_a_maturity_only_investor_is_untouched(self):
        inv = self._investor(date(2026, 7, 1), payout='maturity')
        generate_payout_schedule(inv)
        rows = list(Payout.objects.filter(investor=inv).values_list('payout_type', 'amount_due'))
        self.assertEqual(rows, [('maturity', Decimal('1150000.00'))])
