"""What is falling due, by month — Club 1000's version of AR's same chart.

The columns are this month, the next two, then everything later, then anything
undated. They are built from today's date rather than stored, so they move with
the calendar on their own: in November the first column is Nov. That is the
property worth pinning, because nothing about it is visible until a month rolls
over and the dashboard quietly shows the wrong three months.

Mirrors receivables/engine.py::compute, including the rule that an instalment
whose date has passed is overdue rather than "falling due".
"""
from datetime import date, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Payout, Scheme
from club1000.views import _payout_forecast
from companies.models import Company


class PayoutForecastTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='PFC', name='Forecast Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@pfc.com', company=cls.co, user_code='PFC001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])
        cls.scheme = Scheme.objects.create(
            company=cls.co, name='RISE', tenure_months=12, min_ticket_size=1,
            interest_payout_options=['quarterly'])
        cls.investor = Investor.objects.create(
            company=cls.co, scheme=cls.scheme, added_by=cls.admin,
            name='An Investor', phone='+919800000021',
            amount_invested=Decimal('1000000'), total_return_pct=Decimal('15'),
            investment_date=date(2026, 1, 1), maturity_date=date(2027, 1, 1),
            status='active', approval_status='approved')

    def _payout(self, due, amount='1000', status='pending'):
        return Payout.objects.create(
            investor=self.investor, payout_type='interest', due_date=due,
            amount_due=Decimal(amount), status=status)

    def _forecast(self, as_of):
        return _payout_forecast(Investor.objects.filter(pk=self.investor.pk), as_of)

    def _labels(self, as_of):
        return [b['label'] for b in self._forecast(as_of)]

    def _amount(self, as_of, label):
        return next((b['amount'] for b in self._forecast(as_of) if b['label'] == label), None)

    # --------------------------------------------------------- the columns

    def test_the_columns_are_this_month_and_the_next_two(self):
        self.assertEqual(self._labels(date(2026, 10, 16)),
                         ['Oct-26', 'Nov-26', 'Dec-26', 'After Dec-26'])

    def test_the_columns_move_with_the_calendar(self):
        """The whole reason this is computed rather than stored. Nothing here
        changes but the date, and all four labels must follow."""
        self.assertEqual(self._labels(date(2026, 11, 1)),
                         ['Nov-26', 'Dec-26', 'Jan-27', 'After Jan-27'])
        self.assertEqual(self._labels(date(2027, 2, 9)),
                         ['Feb-27', 'Mar-27', 'Apr-27', 'After Apr-27'])

    def test_the_window_rolls_over_a_year_end(self):
        self.assertEqual(self._labels(date(2026, 12, 31)),
                         ['Dec-26', 'Jan-27', 'Feb-27', 'After Feb-27'])

    # ------------------------------------------------------- what lands where

    def test_a_payout_lands_in_its_own_month(self):
        self._payout(date(2026, 11, 20), '5000')
        as_of = date(2026, 10, 16)
        self.assertEqual(self._amount(as_of, 'Nov-26'), Decimal('5000'))
        self.assertEqual(self._amount(as_of, 'Oct-26'), Decimal('0'))

    def test_several_in_one_month_are_added_up(self):
        self._payout(date(2026, 11, 2), '5000')
        self._payout(date(2026, 11, 27), '2500')
        self.assertEqual(self._amount(date(2026, 10, 16), 'Nov-26'), Decimal('7500'))

    def test_anything_beyond_the_window_falls_into_after(self):
        self._payout(date(2027, 6, 10), '9000')
        self.assertEqual(self._amount(date(2026, 10, 16), 'After Dec-26'), Decimal('9000'))

    def test_an_overdue_payout_is_not_falling_due(self):
        """It is a different question — what has slipped, not what is coming —
        and AR draws the same line."""
        self._payout(date(2026, 9, 1), '4000')
        self.assertEqual(sum(b['amount'] for b in self._forecast(date(2026, 10, 16))),
                         Decimal('0'))

    def test_a_payout_due_today_counts_as_overdue_not_upcoming(self):
        # Matching AR exactly, where `due <= as_of` is overdue.
        self._payout(date(2026, 10, 16), '4000')
        self.assertEqual(self._amount(date(2026, 10, 16), 'Oct-26'), Decimal('0'))

    def test_a_payout_later_this_month_does_count(self):
        self._payout(date(2026, 10, 31), '4000')
        self.assertEqual(self._amount(date(2026, 10, 16), 'Oct-26'), Decimal('4000'))

    def test_a_paid_payout_is_not_counted(self):
        self._payout(date(2026, 11, 20), '5000', status='paid')
        self.assertEqual(self._amount(date(2026, 10, 16), 'Nov-26'), Decimal('0'))

    # ------------------------------------------------------------- the edges

    def test_no_date_column_is_absent_when_every_payout_has_a_date(self):
        """due_date is non-null, so an always-empty column would be clutter —
        it appears only if an undated payout ever exists."""
        self._payout(date(2026, 11, 20), '5000')
        self.assertNotIn('No date', self._labels(date(2026, 10, 16)))

    def test_an_empty_book_still_returns_the_four_columns(self):
        # The chart should render its months with zeros, not vanish.
        rows = self._forecast(date(2026, 10, 16))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r['amount'] == Decimal('0') for r in rows))

    def test_another_company_is_never_counted(self):
        other = Company.objects.create(code='PFD', name='Elsewhere', is_active=True)
        o_admin = User.objects.create_user(
            'a@pfd.com', company=other, user_code='PFD001', password='p',
            name='Other', role='Admin', modules=['Club 1000'])
        o_scheme = Scheme.objects.create(
            company=other, name='THEIRS', tenure_months=12, min_ticket_size=1,
            interest_payout_options=['maturity'])
        o_inv = Investor.objects.create(
            company=other, scheme=o_scheme, added_by=o_admin, name='Theirs',
            phone='+919800000099', amount_invested=Decimal('1'),
            investment_date=date(2026, 1, 1), status='active', approval_status='approved')
        Payout.objects.create(investor=o_inv, payout_type='interest',
                              due_date=date(2026, 11, 20), amount_due=Decimal('999999'),
                              status='pending')
        self.assertEqual(self._amount(date(2026, 10, 16), 'Nov-26'), Decimal('0'))

    # ------------------------------------------------------------ on the API

    def test_the_dashboard_serves_it(self):
        self._payout(date(2026, 11, 20), '5000')
        api = APIClient()
        api.force_authenticate(user=self.admin)
        r = api.get('/api/club1000/stats/')
        self.assertEqual(r.status_code, 200, r.data)
        rows = r.data['payout_forecast']
        self.assertEqual(len(rows), 4)
        # Built from the real today, so assert the shape rather than the labels.
        today = date.today()
        self.assertEqual(rows[0]['label'], today.strftime('%b-%y'))
        self.assertEqual(rows[-1]['label'],
                         'After ' + (today + relativedelta(months=2)).strftime('%b-%y'))
