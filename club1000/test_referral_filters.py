"""The referral ledger, narrowed the way it is read.

Three cards sat above this table — one per referrer, each with a count and an
amount — and none of them could be clicked. The figures counted rows the table
was not showing, which is the same disagreement the Club 1000 dashboard tiles
had before ?bucket= let them be opened.

reference_name, reference_phone and the investor's name are all encrypted, so
neither the referrer filter nor the search can be expressed in SQL and there is
no blind index here to lean on. Both are matched in Python over the
already-scoped rows — these tests exist because that fallback is easy to break
silently: a regression returns an empty list, not an error.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, ReferralReward, Scheme
from companies.models import Company


class ReferralFilterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='RFF', name='Referral Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@rff.com', company=cls.co, user_code='RFF001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])
        cls.rise = Scheme.objects.create(
            company=cls.co, name='RISE', tenure_months=12, min_ticket_size=100000,
            interest_payout_options=['maturity'])
        cls.grow = Scheme.objects.create(
            company=cls.co, name='GROW', tenure_months=24, min_ticket_size=100000,
            interest_payout_options=['maturity'])

        def investor(name, scheme):
            return Investor.objects.create(
                company=cls.co, scheme=scheme, added_by=cls.admin, name=name,
                phone='+9198400000' + str(abs(hash(name)) % 100).zfill(2),
                amount_invested=1000000, investment_date=date.today(),
                status='active', approval_status='approved')

        cls.dilpesh = investor('Dilpesh Kalal', cls.rise)
        cls.ilaben = investor('Ilaben Shah', cls.rise)
        cls.mohini = investor('Mohini Mam', cls.grow)

        def reward(inv, ref_name, ref_phone, amount, pct='0.50', st='pending'):
            return ReferralReward.objects.create(
                investor=inv, reference_name=ref_name, reference_phone=ref_phone,
                amount=Decimal(amount), pct=Decimal(pct), status=st)

        # SM referred two, CJ one, VS one.
        cls.sm_a = reward(cls.dilpesh, 'SM', '8866339595', '5500')
        cls.sm_b = reward(cls.ilaben, 'SM', '+91 88663 39595', '12500')
        cls.cj = reward(cls.mohini, 'CJ', '9998686424', '62500', st='paid')
        # A renewal, which is the only thing the 0.25% rate records.
        cls.vs = reward(cls.mohini, 'VS', '9601250395', '2500', pct='0.25')

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _ids(self, qs=''):
        r = self.api.get(f'/api/club1000/referral-rewards/{qs}')
        self.assertEqual(r.status_code, 200, r.data)
        return {x['id'] for x in r.data}

    # ------------------------------------------------------------ the referrer

    def test_a_referrer_narrows_to_only_their_rewards(self):
        self.assertEqual(self._ids('?reference=8866339595'), {self.sm_a.id, self.sm_b.id})

    def test_the_referrer_matches_on_digits_not_formatting(self):
        """SM's two rewards were typed differently — 8866339595 and
        +91 88663 39595. A referrer is one person, so both must come back."""
        self.assertEqual(self._ids('?reference=%2B91+88663+39595'), {self.sm_a.id, self.sm_b.id})

    def test_a_referrer_with_one_reward_returns_just_that_one(self):
        self.assertEqual(self._ids('?reference=9601250395'), {self.vs.id})

    def test_an_unknown_referrer_returns_nothing_rather_than_everything(self):
        # A filter that silently fell through would show the whole ledger as
        # though it belonged to one person.
        self.assertEqual(self._ids('?reference=9999999999'), set())

    # ---------------------------------------------------------------- the rate

    def test_the_rate_tells_a_new_investment_from_a_renewal(self):
        self.assertEqual(self._ids('?pct=0.25'), {self.vs.id})
        self.assertEqual(self._ids('?pct=0.50'), {self.sm_a.id, self.sm_b.id, self.cj.id})

    def test_a_rate_that_is_not_a_number_is_ignored_rather_than_erroring(self):
        self.assertEqual(len(self._ids('?pct=abc')), 4)

    # -------------------------------------------------------------- the search

    def test_search_matches_the_referrer_name(self):
        self.assertEqual(self._ids('?search=sm'), {self.sm_a.id, self.sm_b.id})

    def test_search_matches_the_referred_investor(self):
        # All three fields are encrypted; this is the Python fallback, and a
        # regression here looks like "search is broken", not like an error.
        self.assertEqual(self._ids('?search=dilpesh'), {self.sm_a.id})

    def test_search_matches_a_referrer_phone(self):
        self.assertEqual(self._ids('?search=9998686424'), {self.cj.id})

    def test_search_that_matches_nobody_returns_nothing(self):
        self.assertEqual(self._ids('?search=zzzznobody'), set())

    # ------------------------------------------------------- the other filters

    def test_status_still_narrows_the_list(self):
        self.assertEqual(self._ids('?status=paid'), {self.cj.id})

    def test_scheme_narrows_the_list(self):
        self.assertEqual(self._ids(f'?scheme_id={self.grow.id}'), {self.cj.id, self.vs.id})

    def test_the_date_range_is_read_against_when_it_was_earned(self):
        today = date.today().isoformat()
        self.assertEqual(len(self._ids(f'?date_from={today}&date_to={today}')), 4)
        old = (date.today() - timedelta(days=30)).isoformat()
        self.assertEqual(self._ids(f'?date_from={old}&date_to={old}'), set())

    def test_the_filters_combine(self):
        self.assertEqual(self._ids('?reference=8866339595&search=dilpesh'), {self.sm_a.id})

    def test_a_referrer_and_a_status_together(self):
        # SM has nothing paid, so this is empty rather than SM's pending two.
        self.assertEqual(self._ids('?reference=8866339595&status=paid'), set())

    # --------------------------------------------------------------- the facets

    def test_the_pickers_offer_only_values_that_are_there(self):
        r = self.api.get('/api/club1000/referral-rewards/?facets=1')
        self.assertEqual(r.status_code, 200)
        f = r.json()
        self.assertEqual(f['statuses'], ['paid', 'pending'])
        self.assertEqual(f['pcts'], ['0.25', '0.50'])
        self.assertEqual(set(f['scheme_ids']), {str(self.rise.id), str(self.grow.id)})

    def test_the_referrer_list_holds_each_person_once(self):
        """SM's two rewards spell the same number two ways. Grouping on the
        digits keeps one entry; grouping on the raw text would offer two."""
        f = self.api.get('/api/club1000/referral-rewards/?facets=1').json()
        self.assertEqual([r['name'] for r in f['referrers']], ['CJ', 'SM', 'VS'])

    def test_facets_answer_for_the_rows_asked_about(self):
        f = self.api.get('/api/club1000/referral-rewards/?facets=1&status=paid').json()
        self.assertEqual([r['name'] for r in f['referrers']], ['CJ'])
        self.assertEqual(f['pcts'], ['0.50'])

    # ------------------------------------------------------------------ scoping

    def test_another_company_is_never_listed(self):
        other = Company.objects.create(code='RFG', name='Other Co', is_active=True)
        o_admin = User.objects.create_user(
            'a@rfg.com', company=other, user_code='RFG001', password='p',
            name='Other Admin', role='Admin', modules=['Club 1000'])
        o_scheme = Scheme.objects.create(
            company=other, name='THEIRS', tenure_months=12, min_ticket_size=1,
            interest_payout_options=['maturity'])
        o_inv = Investor.objects.create(
            company=other, scheme=o_scheme, added_by=o_admin, name='Dilpesh Elsewhere',
            phone='+919840009999', amount_invested=1, investment_date=date.today(),
            status='active', approval_status='approved')
        theirs = ReferralReward.objects.create(
            investor=o_inv, reference_name='SM', reference_phone='8866339595',
            amount=Decimal('1'), pct=Decimal('0.50'), status='pending')
        # Not by a referrer phone shared across two companies, not by name.
        self.assertNotIn(theirs.id, self._ids('?reference=8866339595'))
        self.assertNotIn(theirs.id, self._ids('?search=dilpesh'))
        self.assertNotIn(theirs.id, self._ids())
