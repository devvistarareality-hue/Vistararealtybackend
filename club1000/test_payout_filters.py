"""The payouts ledger answers questions, not just "show me everything".

One status dropdown over an undivided list meant the two things anyone actually
opens this screen for — what has slipped past its due date, and what lands in
the next month — had to be found by eye. Neither is a stored column: both are
the due date read against today, so they are computed the same way here as the
dashboard counts them.

The rest (type, scheme, due-date range, investor name) are the cuts the Club
1000 dashboard already makes its payout figures along, so a tile's number has
a list that matches it.
"""
from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Payout, Scheme
from companies.models import Company


class PayoutFilterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='PYF', name='Payout Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@pyf.com', company=cls.co, user_code='PYF001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])
        cls.rise = Scheme.objects.create(
            company=cls.co, name='RISE', tenure_months=12, min_ticket_size=100000,
            interest_payout_options=['maturity'])
        cls.grow = Scheme.objects.create(
            company=cls.co, name='GROW', tenure_months=24, min_ticket_size=100000,
            interest_payout_options=['quarterly'])
        today = date.today()

        def investor(name, phone, scheme):
            return Investor.objects.create(
                company=cls.co, scheme=scheme, added_by=cls.admin, name=name, phone=phone,
                amount_invested=1000000, investment_date=today - timedelta(days=400),
                maturity_date=today + timedelta(days=100),
                status='active', approval_status='approved')

        cls.vishwas = investor('Vishwas Ghanwat', '+919820000001', cls.rise)
        cls.jharna = investor('Jharna Chakraborty', '+919820000002', cls.grow)

        def payout(inv, days, kind='interest', st='pending'):
            return Payout.objects.create(
                investor=inv, payout_type=kind, due_date=today + timedelta(days=days),
                amount_due=50000, status=st)

        # Slipped past its date and still unpaid — the first question.
        cls.late = payout(cls.vishwas, -40)
        cls.late_today = payout(cls.vishwas, 0)
        # Lands inside the next 30 days — the second.
        cls.soon = payout(cls.jharna, 12, kind='maturity')
        # Neither: too far out, or already dealt with.
        cls.far = payout(cls.jharna, 120)
        cls.old_paid = payout(cls.vishwas, -90, st='paid')

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _ids(self, qs=''):
        r = self.api.get(f'/api/club1000/payouts/{qs}')
        self.assertEqual(r.status_code, 200, r.data)
        return {p['id'] for p in r.data}

    # ------------------------------------------------------------- the buckets

    def test_overdue_is_what_has_slipped_and_is_still_unpaid(self):
        self.assertEqual(self._ids('?bucket=overdue'), {self.late.id})

    def test_something_due_today_is_not_overdue_yet(self):
        """The day it is due, it is due — not late. Marking it late would send
        someone chasing a payment that is not yet owed."""
        self.assertNotIn(self.late_today.id, self._ids('?bucket=overdue'))

    def test_a_payout_already_made_is_never_overdue(self):
        # 90 days past its date, but it was paid. Nothing to chase.
        self.assertNotIn(self.old_paid.id, self._ids('?bucket=overdue'))

    def test_due_soon_is_the_next_thirty_days(self):
        self.assertEqual(self._ids('?bucket=due_soon'), {self.late_today.id, self.soon.id})

    def test_something_already_past_is_not_due_soon(self):
        self.assertNotIn(self.late.id, self._ids('?bucket=due_soon'))

    def test_something_far_out_is_not_due_soon(self):
        self.assertNotIn(self.far.id, self._ids('?bucket=due_soon'))

    def test_an_unknown_bucket_is_ignored_rather_than_emptying_the_list(self):
        # A typo in a link should not read as "there are no payouts".
        self.assertEqual(len(self._ids('?bucket=nonsense')), 5)

    # ------------------------------------------------------------- the filters

    def test_type_narrows_the_list(self):
        self.assertEqual(self._ids('?payout_type=maturity'), {self.soon.id})

    def test_scheme_narrows_the_list(self):
        self.assertEqual(self._ids(f'?scheme_id={self.grow.id}'), {self.soon.id, self.far.id})

    def test_the_date_range_is_read_against_the_due_date(self):
        today = date.today()
        frm = (today + timedelta(days=5)).isoformat()
        to = (today + timedelta(days=30)).isoformat()
        self.assertEqual(self._ids(f'?date_from={frm}&date_to={to}'), {self.soon.id})

    def test_status_still_narrows_the_list(self):
        self.assertEqual(self._ids('?status=paid'), {self.old_paid.id})

    def test_the_filters_combine(self):
        self.assertEqual(
            self._ids(f'?bucket=due_soon&scheme_id={self.grow.id}'), {self.soon.id})

    # -------------------------------------------------------------- the search

    def test_search_matches_the_investor_name(self):
        # investor name is encrypted, so SQL cannot match it — this is the
        # Python fallback, and a regression here looks like "search is broken".
        self.assertEqual(self._ids('?search=jharna'), {self.soon.id, self.far.id})

    def test_search_matches_a_full_phone_number_through_the_blind_index(self):
        self.assertEqual(self._ids('?search=9820000002'), {self.soon.id, self.far.id})

    def test_search_that_matches_nobody_returns_nothing(self):
        self.assertEqual(self._ids('?search=zzzznobody'), set())

    def test_search_narrows_within_a_bucket_rather_than_replacing_it(self):
        self.assertEqual(self._ids('?bucket=due_soon&search=jharna'), {self.soon.id})

    # --------------------------------------------------------------- the facets

    def test_the_pickers_offer_only_values_that_are_there(self):
        # Otherwise a filter can be chosen that empties the list for no reason.
        r = self.api.get('/api/club1000/payouts/?facets=1')
        self.assertEqual(r.status_code, 200)
        f = r.json()
        self.assertEqual(f['statuses'], ['paid', 'pending'])
        self.assertEqual(f['types'], ['interest', 'maturity'])
        self.assertEqual(set(f['scheme_ids']), {str(self.rise.id), str(self.grow.id)})

    def test_facets_answer_for_the_rows_asked_about(self):
        """Facets are computed after the other filters, so a narrowed request
        describes its own result rather than the whole ledger."""
        r = self.api.get(f'/api/club1000/payouts/?facets=1&scheme_id={self.grow.id}')
        self.assertEqual(r.json()['types'], ['interest', 'maturity'])
        r = self.api.get(f'/api/club1000/payouts/?facets=1&scheme_id={self.rise.id}')
        self.assertEqual(r.json()['types'], ['interest'])

    # ------------------------------------------------------------------ scoping

    def test_another_company_is_never_listed(self):
        other = Company.objects.create(code='PYG', name='Other Co', is_active=True)
        o_admin = User.objects.create_user(
            'a@pyg.com', company=other, user_code='PYG001', password='p',
            name='Other Admin', role='Admin', modules=['Club 1000'])
        o_scheme = Scheme.objects.create(
            company=other, name='THEIRS', tenure_months=12, min_ticket_size=1,
            interest_payout_options=['maturity'])
        o_inv = Investor.objects.create(
            company=other, scheme=o_scheme, added_by=o_admin, name='Jharna Elsewhere',
            phone='+919820000002', amount_invested=1, investment_date=date.today(),
            status='active', approval_status='approved')
        theirs = Payout.objects.create(
            investor=o_inv, payout_type='interest', due_date=date.today() - timedelta(days=5),
            amount_due=1, status='pending')
        # Not by bucket, not by a phone shared across two companies, not by name.
        self.assertNotIn(theirs.id, self._ids('?bucket=overdue'))
        self.assertNotIn(theirs.id, self._ids('?search=9820000002'))
        self.assertNotIn(theirs.id, self._ids('?search=jharna'))
