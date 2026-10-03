"""One channel partner per contact number.

The directory had no duplicate check at all, so the same broker could be added
twice — and once they are, their leads and their activity split across two rows
that look identical in every list.

Matching is on contact_key, the HMAC of the last ten digits, so the same number
typed three different ways is still one partner. That matters more here than
almost anywhere: a broker's number gets re-typed by whoever happens to meet them.

The form checks while the number is typed, but every test here goes through the
API, because the form is not the only way in and two people adding the same
broker at the same moment would both pass a client-side check.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import ChannelPartner
from sales.tests import auth

LIST = '/api/sales/channel-partners/'
LOOKUP = '/api/sales/channel-partners/lookup/'


class PartnerDuplicatePhoneTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CPD', name='Dup Co')
        cls.other_co = Company.objects.create(code='CPE', name='Other Co')
        cls.admin = User.objects.create(
            email='cpd_admin@x.com', company=cls.co, role='Admin', designation='Admin',
            user_code='Q0', name='Admin')
        cls.exec_ = User.objects.create(
            email='cpd_exec@x.com', company=cls.co, role='Employee',
            designation='CP EXECUTIVE', user_code='Q1', name='CP Exec')
        cls.stm = User.objects.create(
            email='cpd_stm@x.com', company=cls.co, role='Employee', designation='STM',
            user_code='Q2', name='Plain STM')

    def setUp(self):
        cache.clear()
        auth(self.client, self.exec_)
        self.existing = ChannelPartner.objects.create(
            company=self.co, name='Ramesh Shah', firm_name='Shah Realty',
            contact_no='+919876543210')

    def _add(self, **kw):
        body = {'name': 'New Partner', 'contact_no': '9999999999'}
        body.update(kw)
        return self.client.post(LIST, body, format='json')

    # ------------------------------------------------------------- the block

    def test_a_second_partner_on_the_same_number_is_refused(self):
        r = self._add(name='Ramesh S.', contact_no='+919876543210')
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(ChannelPartner.objects.filter(company=self.co).count(), 1)

    def test_the_refusal_names_who_already_holds_it(self):
        # Without the name this is just "duplicate" and the user has no idea who.
        r = self._add(contact_no='+919876543210')
        self.assertIn('Ramesh Shah', r.data['detail'])
        self.assertIn('Shah Realty', r.data['detail'])
        self.assertEqual(r.data['existing']['id'], self.existing.id)
        self.assertEqual(r.data['existing']['name'], 'Ramesh Shah')

    def test_the_same_number_written_differently_is_still_the_same_partner(self):
        # The whole reason this matches on the last ten digits.
        for variant in ('9876543210', '919876543210', '+91 98765 43210',
                        '098765 43210', '98765-43210'):
            r = self._add(contact_no=variant)
            self.assertEqual(r.status_code, 409, f'{variant} slipped through')
        self.assertEqual(ChannelPartner.objects.filter(company=self.co).count(), 1)

    def test_a_different_number_is_added_normally(self):
        r = self._add(name='Priya Mehta', contact_no='9000011111')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(ChannelPartner.objects.filter(company=self.co).count(), 2)

    def test_another_companys_partner_is_not_a_clash(self):
        ChannelPartner.objects.create(company=self.other_co, name='Elsewhere',
                                      contact_no='9000022222')
        r = self._add(contact_no='9000022222')
        self.assertEqual(r.status_code, 201, 'a different company is a different directory')

    # ---------------------------------------------------------------- lookup

    def test_the_lookup_names_the_existing_partner(self):
        r = self.client.get(LOOKUP, {'contact_no': '98765 43210'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['exists'])
        self.assertEqual(r.data['name'], 'Ramesh Shah')
        self.assertEqual(r.data['firm_name'], 'Shah Realty')

    def test_the_lookup_is_quiet_about_a_free_number(self):
        r = self.client.get(LOOKUP, {'contact_no': '9000033333'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data['exists'])
        self.assertNotIn('name', r.data)

    def test_a_half_typed_number_matches_nothing(self):
        # The field is checked as it is typed, so most calls are partial input.
        for partial in ('', '9', '98765'):
            r = self.client.get(LOOKUP, {'contact_no': partial})
            self.assertEqual(r.status_code, 200, partial)
            self.assertFalse(r.data['exists'], f'{partial!r} matched something')

    def test_the_lookup_does_not_leak_another_companys_directory(self):
        ChannelPartner.objects.create(company=self.other_co, name='Secret Broker',
                                      contact_no='9000044444')
        r = self.client.get(LOOKUP, {'contact_no': '9000044444'})
        self.assertFalse(r.data['exists'])

    def test_the_lookup_is_closed_to_people_outside_the_module(self):
        auth(self.client, self.stm)
        self.assertEqual(self.client.get(LOOKUP, {'contact_no': '9876543210'}).status_code, 403)

    # ------------------------------------------------------------------ edit

    def test_saving_a_partner_without_changing_the_number_is_fine(self):
        # Itself must be excluded, or every edit reports the partner as their own
        # duplicate and the directory becomes read-only.
        r = self.client.patch(f'{LIST}{self.existing.id}/',
                              {'firm_name': 'Shah Realty LLP'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_re_sending_the_same_number_on_an_edit_is_fine(self):
        r = self.client.patch(f'{LIST}{self.existing.id}/',
                              {'contact_no': '+919876543210', 'city': 'Surat'},
                              format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_editing_a_number_onto_another_partners_is_refused(self):
        other = ChannelPartner.objects.create(company=self.co, name='Priya Mehta',
                                              contact_no='9000055555')
        r = self.client.patch(f'{LIST}{other.id}/',
                              {'contact_no': '9876543210'}, format='json')
        self.assertEqual(r.status_code, 409, r.data)
        other.refresh_from_db()
        self.assertEqual(other.contact_no, '9000055555', 'the number must not have moved')

    def test_the_lookup_can_exclude_the_partner_being_edited(self):
        r = self.client.get(LOOKUP, {'contact_no': '9876543210',
                                     'exclude': self.existing.id})
        self.assertFalse(r.data['exists'], 'a partner is not their own duplicate')

    def test_a_junk_exclude_is_ignored_rather_than_crashing(self):
        for bad in ('', 'undefined', 'abc'):
            r = self.client.get(LOOKUP, {'contact_no': '9876543210', 'exclude': bad})
            self.assertEqual(r.status_code, 200, bad)
            self.assertTrue(r.data['exists'], bad)
