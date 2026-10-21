"""A channel partner's Type: A (Wholesale), B (Retail), C (Non CP).

A third grading, not a rename of the two the directory already had. The three
are easy to confuse and were worth pinning down in tests, because they answer
different questions:

    category  the tier they are graded into       platinum / gold / diamond
    segment   what they sell                       residential / industrial / both
    type      the footing we deal with them on     A / B / C

So a partner can be a Platinum Residential A, and filtering by one must not move
the others. Blank is a real value here — the partners already on the directory
have never been graded, and the field must not quietly make them all A.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import ChannelPartner
from sales.tests import auth

LIST = '/api/sales/channel-partners/'


class PartnerTypeTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CPT', name='Type Co')
        cls.other_co = Company.objects.create(code='CPU', name='Other Type Co')
        cls.exec_ = User.objects.create(
            email='cpt_exec@x.com', company=cls.co, role='Employee',
            designation='CP EXECUTIVE', user_code='T1', name='CP Exec')

    def setUp(self):
        cache.clear()
        auth(self.client, self.exec_)

    def _add(self, **kw):
        body = {'name': 'New Partner', 'contact_no': '9000000001'}
        body.update(kw)
        return self.client.post(LIST, body, format='json')

    def _mk(self, **kw):
        kw.setdefault('company', self.co)
        kw.setdefault('name', 'P')
        kw.setdefault('contact_no', '90000%05d' % ChannelPartner.objects.count())
        return ChannelPartner.objects.create(**kw)

    # ------------------------------------------------------- saving the field

    def test_a_partner_can_be_added_as_wholesale(self):
        r = self._add(cp_type='A')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['cp_type'], 'A')
        self.assertEqual(ChannelPartner.objects.get(pk=r.data['id']).cp_type, 'A')

    def test_all_three_bands_are_accepted(self):
        for n, band in enumerate('ABC'):
            r = self._add(cp_type=band, contact_no='91000000%02d' % n)
            self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual(r.data['cp_type'], band)

    def test_a_fourth_band_is_refused(self):
        r = self._add(cp_type='D')
        self.assertEqual(r.status_code, 400, r.data)

    def test_a_partner_added_without_a_type_is_left_ungraded(self):
        """Not defaulted to A — nobody said this partner was wholesale."""
        r = self._add()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['cp_type'], '')

    def test_an_existing_partner_can_be_graded_later(self):
        cp = self._mk(name='Ungraded', contact_no='9333333333')
        r = self.client.patch(f'{LIST}{cp.id}/', {'cp_type': 'B'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        cp.refresh_from_db()
        self.assertEqual(cp.cp_type, 'B')

    def test_grading_a_partner_leaves_their_number_alone(self):
        """A PATCH that doesn't touch the number must not trip the duplicate
        rule by reporting the partner as their own clash."""
        cp = self._mk(name='Keeps Number', contact_no='9444444444')
        r = self.client.patch(f'{LIST}{cp.id}/', {'cp_type': 'C'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        cp.refresh_from_db()
        self.assertEqual(cp.contact_no, '9444444444')

    # ------------------------------------------------------------ filtering

    def test_filtering_by_type_returns_only_that_band(self):
        self._mk(name='Whole', cp_type='A')
        self._mk(name='Retail', cp_type='B')
        self._mk(name='NonCp', cp_type='C')
        r = self.client.get(LIST, {'cp_type': 'B'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c['name'] for c in r.data], ['Retail'])

    def test_no_type_filter_returns_every_band_including_ungraded(self):
        self._mk(name='Whole', cp_type='A')
        self._mk(name='Ungraded')
        r = self.client.get(LIST)
        self.assertEqual(sorted(c['name'] for c in r.data), ['Ungraded', 'Whole'])

    def test_an_ungraded_partner_is_in_no_band(self):
        self._mk(name='Ungraded')
        for band in 'ABC':
            r = self.client.get(LIST, {'cp_type': band})
            self.assertEqual(list(r.data), [], f'band {band}')

    def test_type_and_category_are_different_gradings(self):
        """The bug this guards: wiring Type onto the Category column, so that
        filtering one silently filters the other."""
        self._mk(name='PlatinumA', cp_type='A', category='platinum')
        self._mk(name='GoldA',     cp_type='A', category='gold')
        self._mk(name='PlatinumB', cp_type='B', category='platinum')

        by_type = self.client.get(LIST, {'cp_type': 'A'})
        self.assertEqual(sorted(c['name'] for c in by_type.data), ['GoldA', 'PlatinumA'])

        by_cat = self.client.get(LIST, {'category': 'platinum'})
        self.assertEqual(sorted(c['name'] for c in by_cat.data), ['PlatinumA', 'PlatinumB'])

    def test_type_and_segment_narrow_together(self):
        self._mk(name='Hit',       cp_type='A', segment='industrial')
        self._mk(name='WrongSeg',  cp_type='A', segment='residential')
        self._mk(name='WrongType', cp_type='B', segment='industrial')
        r = self.client.get(LIST, {'cp_type': 'A', 'segment': 'industrial'})
        self.assertEqual([c['name'] for c in r.data], ['Hit'])

    # --------------------------------------------------------------- facets

    def test_the_picker_offers_only_bands_that_are_actually_used(self):
        """Same contract as the other pickers: never offer a choice that
        returns an empty list."""
        self._mk(name='Whole', cp_type='A')
        self._mk(name='NonCp', cp_type='C')
        r = self.client.get(LIST, {'facets': '1'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['cp_types'], ['A', 'C'])

    def test_ungraded_partners_do_not_put_a_blank_in_the_picker(self):
        self._mk(name='Ungraded')
        self._mk(name='Whole', cp_type='A')
        r = self.client.get(LIST, {'facets': '1'})
        self.assertEqual(r.data['cp_types'], ['A'])

    # -------------------------------------------------------------- tenancy

    def test_another_company_s_bands_are_not_offered_or_returned(self):
        self._mk(name='Ours', cp_type='A')
        ChannelPartner.objects.create(
            company=self.other_co, name='Theirs', contact_no='9555555555', cp_type='C')

        rows = self.client.get(LIST, {'cp_type': 'C'})
        self.assertEqual(list(rows.data), [])

        facets = self.client.get(LIST, {'facets': '1'})
        self.assertEqual(facets.data['cp_types'], ['A'])

    # ------------------------------------------------- the Category tiers

    def test_a_partner_can_be_graded_into_each_tier(self):
        for n, tier in enumerate(('platinum', 'gold', 'diamond')):
            r = self._add(category=tier, contact_no='92000000%02d' % n)
            self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual(r.data['category'], tier)

    def test_the_old_tiers_are_gone(self):
        """Premium / Normal / Referral were replaced, not kept alongside."""
        for old in ('premium', 'normal', 'referral'):
            r = self._add(category=old, contact_no='9300000001')
            self.assertEqual(r.status_code, 400, f'{old} was accepted: {r.data}')

    def test_a_partner_added_without_a_tier_is_left_ungraded(self):
        """The old field defaulted to 'normal', which is how 697 partners came
        to be filed as a tier nobody chose. There is no default now."""
        r = self._add()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['category'], '')

    def test_filtering_by_tier_returns_only_that_tier(self):
        self._mk(name='Plat', category='platinum')
        self._mk(name='Gold', category='gold')
        self._mk(name='Dia',  category='diamond')
        self.assertEqual([c['name'] for c in self.client.get(LIST, {'category': 'diamond'}).data],
                         ['Dia'])

    def test_the_tier_picker_offers_only_tiers_in_use(self):
        self._mk(name='Plat', category='platinum')
        self._mk(name='Ungraded')
        f = self.client.get(LIST, {'facets': '1'})
        self.assertEqual(f.data['categories'], ['platinum'])

    def test_the_log_records_the_tier_in_words(self):
        f = ChannelPartner._meta.get_field('category')
        self.assertEqual(dict(f.flatchoices)['diamond'], 'Diamond')
        self.assertFalse(f.has_default(), 'Category must not default to a tier')

    # ---------------------------------------------------------- the Stage

    def test_a_partner_can_be_put_at_each_stage(self):
        for n, stage in enumerate(('new', 'hot', 'warm', 'cold')):
            r = self._add(stage=stage, contact_no='94000000%02d' % n)
            self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual(r.data['stage'], stage)

    def test_a_partner_added_without_a_stage_starts_as_new(self):
        """Unlike Type and Category, Stage has a starting value: a partner
        nobody has read yet is New, and that needs no choosing."""
        r = self._add()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['stage'], 'new')

    def test_a_partner_that_predates_the_field_is_not_called_new(self):
        """The 710 already on the directory were backfilled blank, not New —
        they are not fresh arrivals. Blank must stay a value the API accepts
        and returns, or editing one would silently relabel them."""
        cp = self._mk(name='Legacy', stage='')
        self.assertEqual(cp.stage, '')
        row = next(c for c in self.client.get(LIST).data if c['id'] == cp.id)
        self.assertEqual(row['stage'], '')
        # Editing something else about them leaves the blank alone.
        r = self.client.patch(f'{LIST}{cp.id}/', {'firm_name': 'Legacy Realty'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        cp.refresh_from_db()
        self.assertEqual(cp.stage, '', 'a PATCH must not sweep them into New')

    def test_an_unknown_stage_is_refused(self):
        r = self._add(stage='lukewarm')
        self.assertEqual(r.status_code, 400, r.data)

    def test_filtering_by_stage_returns_only_that_stage(self):
        self._mk(name='Hot', stage='hot')
        self._mk(name='Warm', stage='warm')
        self._mk(name='Cold', stage='cold')
        self.assertEqual([c['name'] for c in self.client.get(LIST, {'stage': 'hot'}).data], ['Hot'])

    def test_the_stage_picker_offers_only_stages_in_use(self):
        self._mk(name='Hot', stage='hot')
        self._mk(name='Legacy', stage='')   # predates the field
        f = self.client.get(LIST, {'facets': '1'})
        self.assertEqual(f.data['stages'], ['hot'], 'blank is not offered as a stage')

    def test_stage_is_about_the_partner_not_their_leads(self):
        """A partner's Stage and a lead's hot/warm/cold are separate fields on
        separate tables. The bug this guards is wiring one to the other: a cold
        partner who happens to bring a hot lead must stay cold."""
        cp = self._mk(name='Cold Partner', stage='cold')
        from sales.models import Lead, LeadSource
        src = LeadSource.objects.create(company=self.co, name='CP')
        Lead.objects.create(company=self.co, name='Hot Buyer', phone='9123456780',
                            source=src, channel_partner=cp, telecaller_status='hot')
        row = next(c for c in self.client.get(LIST).data if c['id'] == cp.id)
        self.assertEqual(row['stage'], 'cold')
        self.assertEqual(row['lead_count'], 1, 'the lead is still counted against them')

    def test_the_four_gradings_narrow_independently(self):
        """Type, Category, Stage and Segment are four separate fields; filtering
        on one must not move the others."""
        self._mk(name='All four', cp_type='A', category='platinum', stage='hot',
                 segment='industrial')
        self._mk(name='Wrong stage', cp_type='A', category='platinum', stage='cold',
                 segment='industrial')
        r = self.client.get(LIST, {'cp_type': 'A', 'category': 'platinum',
                                   'stage': 'hot', 'segment': 'industrial'})
        self.assertEqual([c['name'] for c in r.data], ['All four'])

    def test_another_company_s_stages_are_not_offered(self):
        self._mk(name='Ours', stage='hot')
        ChannelPartner.objects.create(
            company=self.other_co, name='Theirs', contact_no='9556666666', stage='cold')
        self.assertEqual(list(self.client.get(LIST, {'stage': 'cold'}).data), [])
        self.assertEqual(self.client.get(LIST, {'facets': '1'}).data['stages'], ['hot'])

    # ------------------------------------------------------- activity log

    def test_the_log_records_the_band_in_words(self):
        """The field's verbose_name is what the log prints, so it has to read as
        'Type', not 'Cp type', and the value as the full wording."""
        f = ChannelPartner._meta.get_field('cp_type')
        self.assertEqual(str(f.verbose_name), 'Type')
        self.assertEqual(dict(f.flatchoices)['A'], 'A (Wholesale)')

    def test_the_log_records_the_stage_in_words(self):
        f = ChannelPartner._meta.get_field('stage')
        self.assertEqual(str(f.verbose_name), 'Stage')
        self.assertEqual(dict(f.flatchoices)['hot'], 'Hot')
        # Stage is the one of the four that does start somewhere: New.
        self.assertEqual(f.get_default(), 'new')
