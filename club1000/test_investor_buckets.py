"""A dashboard tile opens the rows it counted, not the whole book.

"Due for renewal" and "Maturing in 30 days" count something no stored status
holds: the first is active-past-maturity (Investor.is_matured, computed because
nothing ever flips status to 'matured'), the second a 30-day window on the date.
So ?status= cannot express either, and the tiles used to land on the unfiltered
list — 8 on the tile, 49 rows on the screen.

?bucket= applies the same conditions the dashboard counted with. These tests
assert the tile and the list agree, which is the only property that matters:
they are computed in two places and would otherwise drift apart silently.
"""
from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Scheme
from companies.models import Company


class InvestorBucketTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='BKT', name='Bucket Co', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@bkt.com', company=cls.co, user_code='BKT001', password='p',
            name='Club Admin', role='Admin', modules=['Club 1000'])
        cls.scheme = Scheme.objects.create(
            company=cls.co, name='RISE', tenure_months=12, min_ticket_size=100000,
            interest_payout_options=['maturity'])
        today = date.today()

        def inv(name, matures, status='active', approval='approved'):
            return Investor.objects.create(
                company=cls.co, scheme=cls.scheme, added_by=cls.admin, name=name,
                phone='+9198000006' + str(abs(hash(name)) % 100).zfill(2),
                amount_invested=1000000, investment_date=today - timedelta(days=400),
                maturity_date=matures, status=status, approval_status=approval)

        # Past maturity and still active — the renewal decision nobody has made.
        cls.overdue = inv('Overdue One', today - timedelta(days=30))
        cls.due_today = inv('Due Today', today)
        # Inside the 30-day window.
        cls.soon = inv('Maturing Soon', today + timedelta(days=10))
        # Neither: far off, or already dealt with.
        cls.far = inv('Far Off', today + timedelta(days=200))
        cls.redeemed = inv('Already Redeemed', today - timedelta(days=60), status='redeemed')

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _names(self, qs=''):
        r = self.api.get(f'/api/club1000/investors/{qs}')
        self.assertEqual(r.status_code, 200, r.data)
        rows = r.data if isinstance(r.data, list) else r.data.get('results', [])
        return {i['name'] for i in rows}

    def _tile(self, key):
        r = self.api.get('/api/club1000/stats/')
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    # ----------------------------------------------------------- due for renewal

    def test_due_for_renewal_lists_only_the_overdue(self):
        names = self._names('?bucket=due_for_renewal')
        self.assertEqual(names, {'Overdue One', 'Due Today'})

    def test_maturing_today_counts_as_due(self):
        # is_matured() uses <= today, so the day itself is already a decision.
        self.assertIn('Due Today', self._names('?bucket=due_for_renewal'))

    def test_something_already_redeemed_is_not_due(self):
        # Past its date, but the decision was taken.
        self.assertNotIn('Already Redeemed', self._names('?bucket=due_for_renewal'))

    def test_the_tile_and_the_list_agree(self):
        """The whole point. The count and the rows are computed in two places —
        a dashboard loop in Python, a queryset in SQL — and nothing but this test
        stops them drifting."""
        tile = self._tile('due_for_renewal')['investor_status_breakdown']['due_for_renewal']
        self.assertEqual(tile, len(self._names('?bucket=due_for_renewal')))

    # -------------------------------------------------------------- maturing soon

    def test_maturing_soon_is_the_next_thirty_days(self):
        self.assertEqual(self._names('?bucket=maturing_soon'), {'Maturing Soon', 'Due Today'})

    def test_something_already_past_is_not_maturing_soon(self):
        self.assertNotIn('Overdue One', self._names('?bucket=maturing_soon'))

    def test_maturing_exactly_today_falls_in_both_buckets(self):
        """Not a bug introduced here — the dashboard already counts it twice.

        'Due for renewal' is maturity_date <= today and 'Maturing in 30 days' is
        >= today, so the day itself satisfies both. These filters mirror the
        tiles exactly, which is the property worth having; pinning the overlap
        here means it is known rather than discovered later as a discrepancy.
        """
        self.assertIn('Due Today', self._names('?bucket=due_for_renewal'))
        self.assertIn('Due Today', self._names('?bucket=maturing_soon'))

    def test_something_far_off_is_not_maturing_soon(self):
        self.assertNotIn('Far Off', self._names('?bucket=maturing_soon'))

    def test_the_maturing_tile_and_the_list_agree(self):
        tile = self._tile('upcoming')['upcoming_maturities_count']
        self.assertEqual(tile, len(self._names('?bucket=maturing_soon')))

    # ------------------------------------- the filters the dashboard implies

    def test_the_investment_date_range_narrows_the_list(self):
        """The dashboard narrows its figures by investment_date and the list
        could not, so a tile's total had no list that matched it."""
        today = date.today()
        old = today - timedelta(days=500)
        self.assertEqual(
            self._names(f'?date_from={old.isoformat()}&date_to={old.isoformat()}'), set())
        # Everything here was invested 400 days ago.
        invested = (today - timedelta(days=400)).isoformat()
        self.assertEqual(len(self._names(f'?date_from={invested}&date_to={invested}')), 5)

    def test_payout_narrows_the_list(self):
        # name is encrypted — filter(name=…) would match nothing and update()
        # would touch no rows, silently. Update by primary key.
        Investor.objects.filter(pk=self.overdue.pk).update(interest_payout='quarterly')
        self.assertEqual(self._names('?interest_payout=quarterly'), {'Overdue One'})

    def test_added_by_narrows_the_list(self):
        other = User.objects.create_user(
            'other@bkt.com', company=self.co, user_code='BKT002', password='p',
            name='Someone Else', role='Employee', modules=['Club 1000'])
        Investor.objects.filter(pk=self.far.pk).update(added_by=other)
        self.assertEqual(self._names(f'?added_by={other.id}'), {'Far Off'})

    def test_the_filters_combine(self):
        # A bucket and a payout together, as the screen sends them.
        Investor.objects.filter(pk=self.overdue.pk).update(interest_payout='quarterly')
        self.assertEqual(
            self._names('?bucket=due_for_renewal&interest_payout=quarterly'), {'Overdue One'})

    def test_the_pickers_offer_only_values_that_are_there(self):
        # Otherwise a filter can be chosen that empties the list for no reason.
        r = self.api.get('/api/club1000/investors/?facets=1')
        self.assertEqual(r.status_code, 200)
        f = r.json()
        self.assertEqual(f['payouts'], ['maturity'])
        self.assertEqual([a['name'] for a in f['added_by']], ['Club Admin'])

    # ------------------------------------------------------------------- general

    def test_no_bucket_still_lists_everything(self):
        self.assertEqual(len(self._names()), 5)

    def test_an_unknown_bucket_is_ignored_rather_than_emptying_the_list(self):
        # A typo in a link should not read as "you have no investors".
        self.assertEqual(len(self._names('?bucket=nonsense')), 5)
