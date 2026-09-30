"""Only an approved investor counts as one.

Until someone signs off, the money is not committed: a pending investor may be
rejected, and a draft was never even submitted. Both used to land in
total_invested, the investor count, the per-scheme split and the top-investors
list — and so did a rejected one, which meant a refused deal permanently
overstated the book.

`pending_approval_count` is the deliberate exception: counting what is waiting
for a decision is the whole point of it.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Scheme
from companies.models import Company


class OnlyApprovedInvestorsCount(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CNT', name='Count Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@cnt.com', company=cls.co, user_code='CNT001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])
        cls.scheme = Scheme.objects.create(
            company=cls.co, name='RISE', tenure_months=12, min_ticket_size=100000,
            interest_payout_options=['maturity'])

        def inv(name, amount, approval):
            return Investor.objects.create(
                company=cls.co, scheme=cls.scheme, added_by=cls.admin, name=name,
                phone='+9198000005' + str(abs(hash(name)) % 100).zfill(2),
                amount_invested=amount, investment_date='2026-01-01',
                maturity_date='2027-01-01', approval_status=approval)

        cls.approved = inv('Approved One', 1000000, 'approved')
        cls.pending = inv('Pending One', 80000000, 'pending')
        cls.draft = inv('Draft One', 15000000, 'draft')
        cls.rejected = inv('Rejected One', 50000000, 'rejected')

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _stats(self):
        r = self.api.get('/api/club1000/stats/')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_the_investor_count_is_the_approved_ones_only(self):
        self.assertEqual(self._stats()['investor_count'], 1)

    def test_total_invested_excludes_everything_unapproved(self):
        """The unapproved three are worth 145,000,000 between them — none of it
        is committed money."""
        self.assertEqual(Decimal(str(self._stats()['total_invested'])), Decimal('1000000'))

    def test_the_per_scheme_split_agrees_with_the_headline(self):
        by_scheme = self._stats()['by_scheme']
        self.assertEqual(len(by_scheme), 1)
        self.assertEqual(by_scheme[0]['investors'], 1)
        self.assertEqual(Decimal(str(by_scheme[0]['amount'])), Decimal('1000000'))

    def test_top_investors_does_not_lead_with_an_unapproved_one(self):
        """The pending ₹8cr would otherwise sit at the top of the list."""
        names = [t['name'] for t in self._stats().get('top_investors', [])]
        self.assertNotIn('Pending One', names)
        self.assertNotIn('Draft One', names)
        self.assertNotIn('Rejected One', names)

    def test_the_status_breakdown_counts_only_approved(self):
        breakdown = self._stats()['investor_status_breakdown']
        self.assertEqual(sum(breakdown.values()), 1)

    def test_pending_approval_count_still_counts_what_is_waiting(self):
        """The one figure that is meant to see unapproved rows."""
        self.assertEqual(self._stats()['pending_approval_count'], 1)

    def test_approving_one_makes_it_count(self):
        Investor.objects.filter(pk=self.pending.pk).update(approval_status='approved')
        stats = self._stats()
        self.assertEqual(stats['investor_count'], 2)
        self.assertEqual(Decimal(str(stats['total_invested'])), Decimal('81000000'))
