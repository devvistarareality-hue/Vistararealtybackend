"""CP Details, narrowed the way a directory of 594 is actually read.

One search box over six hundred partners meant everything else — who is still
active, who deals in industrial, who is in Baroda, who we added last month —
had to be found by eye.

Two of these need care. Location is two columns (city and area) holding the
same kind of value, shown together in one column: on the live directory 439 of
594 partners have no city at all and are placed entirely by area, so filtering
on city alone would have missed most of them. And the engagement buckets are
not stored fields at all — they are the annotated lead/follow-up/visit counts,
which is the only way to answer "which of these has nobody ever contacted".
"""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import ChannelPartner, Lead, PartnerFollowUp


class CpDirectoryFilterTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CPD', name='Partner Co', is_active=True)
        cls.other_co = Company.objects.create(code='CPE', name='Elsewhere', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@cpd.com', company=cls.co, user_code='CPD001', password='p',
            name='CP Admin', role='Admin', modules=['Sales', 'Channel Partner'])
        cls.other_user = User.objects.create_user(
            'two@cpd.com', company=cls.co, user_code='CPD002', password='p',
            name='Second Adder', role='Manager', modules=['Sales', 'Channel Partner'])

        def partner(name, **kw):
            kw.setdefault('company', cls.co)
            kw.setdefault('contact_no', '+9198700000' + str(abs(hash(name)) % 100).zfill(2))
            kw.setdefault('created_by', cls.admin)
            return ChannelPartner.objects.create(name=name, **kw)

        # Placed by city only, by area only, and by both — the three shapes the
        # live data actually takes.
        cls.by_city = partner('City Only', city='Ahmedabad', category='platinum',
                              segment='residential', is_active=True)
        cls.by_area = partner('Area Only', area='AHMEDABAD', category='gold',
                              segment='industrial', is_active=False)
        cls.both = partner('Both Fields', city='Baroda', area='BARODA',
                           category='gold', segment='both', is_active=True)
        cls.elsewhere = partner('Far Away', city='KOLKATA', category='diamond',
                                segment='residential', is_active=False,
                                created_by=cls.other_user)
        cls.nowhere = partner('No Place', category='gold', is_active=False)

        # Engagement: one with a lead, one with only a follow-up, three with
        # nothing at all.
        Lead.objects.create(company=cls.co, name='A Lead', phone='+919870001111',
                            channel_partner=cls.by_city)
        PartnerFollowUp.objects.create(
            channel_partner=cls.both, scheduled_at=timezone.now(),
            assigned_to=cls.admin, created_by=cls.admin)

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _names(self, qs=''):
        r = self.api.get(f'/api/sales/channel-partners/{qs}')
        self.assertEqual(r.status_code, 200, r.data)
        return {c['name'] for c in r.data}

    # ------------------------------------------------------------- the columns

    def test_status_narrows_the_list(self):
        self.assertEqual(self._names('?status=active'), {'City Only', 'Both Fields'})
        self.assertEqual(self._names('?status=inactive'),
                         {'Area Only', 'Far Away', 'No Place'})

    def test_category_narrows_the_list(self):
        # Named for Category, not Type: Type is now its own field (A/B/C).
        self.assertEqual(self._names('?category=platinum'), {'City Only'})
        self.assertEqual(self._names('?category=diamond'), {'Far Away'})

    def test_segment_narrows_the_list(self):
        self.assertEqual(self._names('?segment=industrial'), {'Area Only'})

    def test_added_by_narrows_the_list(self):
        self.assertEqual(self._names(f'?created_by={self.other_user.id}'), {'Far Away'})

    def test_the_date_range_is_read_against_when_they_were_added(self):
        today = date.today().isoformat()
        self.assertEqual(len(self._names(f'?date_from={today}&date_to={today}')), 5)
        old = (date.today() - timedelta(days=60)).isoformat()
        self.assertEqual(self._names(f'?date_from={old}&date_to={old}'), set())

    def test_a_date_that_is_not_a_date_narrows_nothing_rather_than_erroring(self):
        self.assertEqual(len(self._names('?date_from=not-a-date')), 5)

    # ------------------------------------------------------------- the location

    def test_location_matches_a_partner_placed_by_city(self):
        self.assertIn('City Only', self._names('?location=Ahmedabad'))

    def test_location_also_matches_a_partner_placed_only_by_area(self):
        """The one that would have been missed. Most of the live directory has
        no city at all — filtering on city alone would have hidden them."""
        self.assertIn('Area Only', self._names('?location=Ahmedabad'))

    def test_location_ignores_case(self):
        # The data holds 'Ahmedabad' and 'AHMEDABAD' for the same place.
        self.assertEqual(self._names('?location=AHMEDABAD'), self._names('?location=ahmedabad'))

    def test_a_partner_is_not_listed_twice_when_city_and_area_both_match(self):
        rows = self.api.get('/api/sales/channel-partners/?location=Baroda').data
        self.assertEqual([c['name'] for c in rows].count('Both Fields'), 1)

    def test_a_partner_with_no_place_is_not_matched_by_a_location(self):
        self.assertNotIn('No Place', self._names('?location=Ahmedabad'))

    # ----------------------------------------------------------- the engagement

    def test_with_leads_is_only_those_that_produced_one(self):
        self.assertEqual(self._names('?bucket=with_leads'), {'City Only'})

    def test_no_leads_is_everyone_else(self):
        self.assertEqual(self._names('?bucket=no_leads'),
                         {'Area Only', 'Both Fields', 'Far Away', 'No Place'})

    def test_never_contacted_excludes_a_partner_that_was_followed_up(self):
        """'Both Fields' has no lead but someone did call them, so they are not
        untouched — that is the distinction the two buckets exist to draw."""
        untouched = self._names('?bucket=untouched')
        self.assertNotIn('Both Fields', untouched)
        self.assertNotIn('City Only', untouched)
        self.assertEqual(untouched, {'Area Only', 'Far Away', 'No Place'})

    def test_an_unknown_bucket_is_ignored_rather_than_emptying_the_list(self):
        self.assertEqual(len(self._names('?bucket=nonsense')), 5)

    # ---------------------------------------------------------------- combining

    def test_the_filters_combine(self):
        self.assertEqual(self._names('?status=inactive&location=Ahmedabad'), {'Area Only'})

    def test_search_still_works_alongside_a_filter(self):
        self.assertEqual(self._names('?status=active&search=city'), {'City Only'})

    # ----------------------------------------------------------------- the facets

    def test_the_pickers_offer_only_values_that_are_there(self):
        r = self.api.get('/api/sales/channel-partners/?facets=1')
        self.assertEqual(r.status_code, 200)
        f = r.json()
        self.assertEqual(f['categories'], ['diamond', 'gold', 'platinum'])
        self.assertEqual(f['segments'], ['both', 'industrial', 'residential'])
        self.assertEqual([u['name'] for u in f['created_by']], ['CP Admin', 'Second Adder'])

    def test_the_location_picker_holds_each_place_once(self):
        """'Ahmedabad' and 'AHMEDABAD' are one place; offering both would split
        a filter that matches either anyway."""
        f = self.api.get('/api/sales/channel-partners/?facets=1').json()
        lowered = [l.lower() for l in f['locations']]
        self.assertEqual(len(lowered), len(set(lowered)), f['locations'])
        self.assertEqual(sorted(lowered), ['ahmedabad', 'baroda', 'kolkata'])

    def test_facets_answer_for_the_rows_asked_about(self):
        f = self.api.get('/api/sales/channel-partners/?facets=1&status=active').json()
        self.assertEqual(f['categories'], ['gold', 'platinum'])

    # ------------------------------------------------------------------ scoping

    def test_another_company_is_never_listed(self):
        theirs = ChannelPartner.objects.create(
            company=self.other_co, name='Their Partner', contact_no='+919870009999',
            city='Ahmedabad', category='gold', is_active=True)
        self.assertNotIn(theirs.name, self._names('?location=Ahmedabad'))
        self.assertNotIn(theirs.name, self._names('?status=active'))
        self.assertNotIn(theirs.name, self._names())
