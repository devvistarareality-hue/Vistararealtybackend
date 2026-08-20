"""Club 1000 PII is encrypted at rest and its searches still work."""
import datetime

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from club1000.models import Investor, Lead, Scheme
from club1000.views import InvestorListCreateView, LeadListCreateView
from companies.models import Company
from sales.fields import phone_blind_index


def raw(table, col, pk):
    with connection.cursor() as c:
        c.execute(f'SELECT {col} FROM {table} WHERE id = %s', [pk])
        return c.fetchone()[0]


class Club1000Encryption(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='C1', name='Club Co')
        self.u = User.objects.create(name='Mgr', email='m@c.com', phone='9000000009',
                                     user_code='M1', role='Admin', company=self.co,
                                     modules=['Club 1000'])
        self.scheme = Scheme.objects.create(company=self.co, name='Gold',
                                            tenure_months=12, min_ticket_size=100000)
        self.inv = Investor.objects.create(
            company=self.co, scheme=self.scheme, name='Meera Shah', phone='+919812345678',
            email='meera@example.com', pan='ABCDE1234F', notes='prefers quarterly',
            amount_invested=500000, investment_date=datetime.date(2026, 1, 1))
        self.lead = Lead.objects.create(company=self.co, name='Rakesh Jain',
                                        phone='9800011122', email='rakesh@example.com',
                                        remarks='asked about tenure')

    def test_investor_pii_is_ciphertext(self):
        for col in ('name', 'phone', 'email', 'pan', 'notes'):
            self.assertTrue(str(raw('club1000_investor', col, self.inv.id)).startswith('gAAAAA'),
                            f'Investor.{col} not encrypted')

    def test_lead_pii_is_ciphertext(self):
        for col in ('name', 'phone', 'email', 'remarks'):
            self.assertTrue(str(raw('club1000_lead', col, self.lead.id)).startswith('gAAAAA'),
                            f'Lead.{col} not encrypted')

    def test_pan_never_appears_in_plaintext(self):
        with connection.cursor() as c:
            c.execute("SELECT count(*) FROM club1000_investor WHERE pan LIKE %s", ['%ABCDE1234F%'])
            self.assertEqual(c.fetchone()[0], 0, 'PAN readable in the database')

    def test_reads_back_plaintext(self):
        i = Investor.objects.get(pk=self.inv.pk)
        self.assertEqual(i.name, 'Meera Shah')
        self.assertEqual(i.pan, 'ABCDE1234F')
        self.assertEqual(Lead.objects.get(pk=self.lead.pk).remarks, 'asked about tenure')

    def test_blind_index_set_on_both_models(self):
        self.assertEqual(self.inv.phone_key, phone_blind_index('9812345678'))
        self.assertEqual(self.lead.phone_key, phone_blind_index('9800011122'))

    def test_scheme_name_stays_plain_so_uniqueness_holds(self):
        self.assertEqual(raw('club1000_scheme', 'name', self.scheme.id), 'Gold')
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Scheme.objects.create(company=self.co, name='Gold',
                                      tenure_months=6, min_ticket_size=1)

    def _search(self, view, term):
        req = APIRequestFactory().get(f'/x/?search={term}')
        force_authenticate(req, user=self.u)
        res = view.as_view()(req)
        rows = res.data.get('results', res.data) if isinstance(res.data, dict) else res.data
        return res.status_code, [r.get('name') for r in rows]

    def test_investor_search_by_name_phone_email(self):
        self.assertEqual(self._search(InvestorListCreateView, 'meera')[1], ['Meera Shah'])
        self.assertEqual(self._search(InvestorListCreateView, '9812345678')[1], ['Meera Shah'])
        self.assertEqual(self._search(InvestorListCreateView, '98123')[1], ['Meera Shah'])
        self.assertEqual(self._search(InvestorListCreateView, 'meera@')[1], ['Meera Shah'])
        self.assertEqual(self._search(InvestorListCreateView, 'nobody')[1], [])

    def test_backfill_converts_legacy_rows(self):
        with connection.cursor() as c:
            c.execute("UPDATE club1000_investor SET pan=%s, phone_key=%s WHERE id=%s",
                      ['ZZZZZ9999Z', '', self.inv.id])
        self.assertEqual(raw('club1000_investor', 'pan', self.inv.id), 'ZZZZZ9999Z')
        call_command('encrypt_existing_pii', verbosity=0)
        self.assertTrue(str(raw('club1000_investor', 'pan', self.inv.id)).startswith('gAAAAA'))
        i = Investor.objects.get(pk=self.inv.pk)
        self.assertEqual(i.pan, 'ZZZZZ9999Z')
        self.assertEqual(i.phone_key, phone_blind_index('9812345678'))
