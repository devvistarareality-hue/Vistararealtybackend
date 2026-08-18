"""Lead PII is encrypted at rest, and the searches that depend on it still work."""
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from companies.models import Company
from sales.fields import phone_blind_index
from sales.models import Lead
from sales.views import LeadListView


def raw(col, pk):
    with connection.cursor() as c:
        c.execute(f'SELECT {col} FROM sales_lead WHERE id = %s', [pk])
        return c.fetchone()[0]


class LeadEncryption(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='ENC', name='Enc Co')
        self.admin = User.objects.create(name='A', email='a@x.com', phone='9000000001',
                                         user_code='A1', role='Admin', company=self.co)
        self.l = Lead.objects.create(company=self.co, name='Ramesh Bhai Patel',
                                     phone='+919876543210', email='ramesh@example.com')

    def tearDown(self):
        cache.clear()

    def _search(self, term):
        req = APIRequestFactory().get(f'/api/sales/leads/?page=1&search={term}')
        force_authenticate(req, user=self.admin)
        res = LeadListView.as_view()(req)
        rows = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        return [r['name'] for r in rows]

    def test_pii_is_ciphertext_at_rest(self):
        for col in ('name', 'phone', 'email'):
            self.assertTrue(str(raw(col, self.l.id)).startswith('gAAAAA'),
                            f'{col} is not encrypted at rest')

    def test_reads_back_plaintext(self):
        f = Lead.objects.get(pk=self.l.pk)
        self.assertEqual(f.name, 'Ramesh Bhai Patel')
        self.assertEqual(f.phone, '+919876543210')
        self.assertEqual(f.email, 'ramesh@example.com')

    def test_blind_index_is_set_and_normalised(self):
        self.assertEqual(self.l.phone_key, phone_blind_index('9876543210'))
        # the same number in three formats yields one key — what endswith used to do
        self.assertEqual(phone_blind_index('+919876543210'),
                         phone_blind_index('919876543210'))
        self.assertEqual(phone_blind_index('9876543210'),
                         phone_blind_index('+91 98765 43210'))
        # and the key is not the number
        self.assertNotIn('9876543210', self.l.phone_key)

    def test_blind_index_updates_when_phone_changes(self):
        self.l.phone = '9000011111'
        self.l.save()
        self.assertEqual(Lead.objects.get(pk=self.l.pk).phone_key,
                         phone_blind_index('9000011111'))

    def test_duplicate_detection_still_matches_across_formats(self):
        for variant in ('9876543210', '+919876543210', '919876543210', '098765 43210'):
            key = phone_blind_index(''.join(c for c in variant if c.isdigit())[-10:])
            self.assertEqual(Lead.objects.filter(company=self.co, phone_key=key).count(), 1,
                             f'duplicate check missed {variant}')

    def test_search_by_full_phone_uses_the_index(self):
        self.assertEqual(self._search('9876543210'), ['Ramesh Bhai Patel'])
        self.assertEqual(self._search('+919876543210'), ['Ramesh Bhai Patel'])

    def test_search_by_partial_phone_still_works(self):
        self.assertEqual(self._search('98765'), ['Ramesh Bhai Patel'])
        self.assertEqual(self._search('543210'), ['Ramesh Bhai Patel'])

    def test_search_by_name_fragment_still_works(self):
        self.assertEqual(self._search('ramesh'), ['Ramesh Bhai Patel'])
        self.assertEqual(self._search('bhai'), ['Ramesh Bhai Patel'])
        self.assertEqual(self._search('PATEL'), ['Ramesh Bhai Patel'])

    def test_search_by_email_still_works(self):
        self.assertEqual(self._search('ramesh@'), ['Ramesh Bhai Patel'])
        self.assertEqual(self._search('example.com'), ['Ramesh Bhai Patel'])

    def test_search_misses_do_not_match(self):
        self.assertEqual(self._search('zzzznothing'), [])
        self.assertEqual(self._search('1231231234'), [])

    def test_search_is_still_company_scoped(self):
        other = Company.objects.create(code='OTH', name='Other')
        Lead.objects.create(company=other, name='Ramesh Other', phone='9876543211')
        self.assertEqual(self._search('ramesh'), ['Ramesh Bhai Patel'])

    def test_backfill_encrypts_legacy_rows_and_fills_the_index(self):
        l = Lead.objects.create(company=self.co, name='Legacy', phone='9111122223')
        with connection.cursor() as c:   # simulate a pre-migration row
            c.execute('UPDATE sales_lead SET name=%s, phone=%s, email=%s, phone_key=%s WHERE id=%s',
                      ['Old Name', '9111122223', 'old@x.com', '', l.id])
        self.assertEqual(raw('name', l.id), 'Old Name')

        call_command('encrypt_existing_pii', verbosity=0)

        self.assertTrue(str(raw('name', l.id)).startswith('gAAAAA'))
        fresh = Lead.objects.get(pk=l.pk)
        self.assertEqual(fresh.name, 'Old Name')
        self.assertEqual(fresh.phone, '9111122223')
        self.assertEqual(fresh.phone_key, phone_blind_index('9111122223'))
        call_command('encrypt_existing_pii', verbosity=0)   # idempotent
        self.assertEqual(Lead.objects.get(pk=l.pk).name, 'Old Name')
