"""Bank Master — run against local SQLite, never the production database:

    DATABASE_URL= DB_ENGINE=django.db.backends.sqlite3 ./venv/bin/python manage.py test receivables
"""
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Project
from receivables.models import ARBank
from receivables.test_api import make_booking


class ARBankTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='VIS', name='Vistara')
        self.other = Company.objects.create(code='OTH', name='Other')
        self.project = Project.objects.create(company=self.co, name='Kalrav 2')
        self.user = User.objects.create_user('ar1@test.local', company=self.co, user_code='AR1', password='x',
                                             name='AR User', role='Employee', modules=['AR', 'Bank Master'])
        make_booking(self.co, self.project)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.aid = self.api.get('/api/ar/accounts/').json()['results'][0]['id']

    def add_bank(self, name='HDFC Current', opening='1000000'):
        r = self.api.post('/api/ar/banks/', {'name': name, 'opening_balance': opening}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()['id']

    def bank(self, bid):
        return next(b for b in self.api.get('/api/ar/banks/').json()['results'] if b['id'] == bid)

    def pay(self, **kw):
        body = {'paid_on': '2025-08-01', 'amount': '250000', 'mode': 'loan', **kw}
        return self.api.post(f'/api/ar/accounts/{self.aid}/receipts/', body, format='json')

    def test_balance_is_opening_plus_loan_receipts(self):
        bid = self.add_bank()
        self.assertEqual(self.bank(bid)['balance'], 1000000)
        self.assertEqual(self.pay(bank=bid).status_code, 201)
        self.assertEqual(self.pay(bank=bid, amount='50000').status_code, 201)
        b = self.bank(bid)
        self.assertEqual((b['opening_balance'], b['received'], b['balance']), (1000000, 300000, 1300000))

    def test_editing_or_deleting_a_receipt_moves_the_balance(self):
        bid = self.add_bank(opening='0')
        rid = self.pay(bank=bid).json()['id']
        self.api.patch(f'/api/ar/receipts/{rid}/', {'amount': '100000'}, format='json')
        self.assertEqual(self.bank(bid)['balance'], 100000)
        self.api.delete(f'/api/ar/receipts/{rid}/')
        self.assertEqual(self.bank(bid)['balance'], 0)

    def test_moving_a_receipt_to_another_bank(self):
        a, b = self.add_bank('HDFC', '0'), self.add_bank('SBI', '0')
        rid = self.pay(bank=a).json()['id']
        self.api.patch(f'/api/ar/receipts/{rid}/', {'bank': b}, format='json')
        self.assertEqual((self.bank(a)['balance'], self.bank(b)['balance']), (0, 250000))

    def test_a_loan_payment_needs_a_bank_and_nbfc_has_none(self):
        bid = self.add_bank()
        r = self.pay()
        self.assertEqual(r.status_code, 400)
        self.assertIn('bank', r.json())
        self.assertEqual(self.pay(mode='nbfc', bank=bid).status_code, 201)
        self.assertEqual(self.bank(bid)['received'], 0, 'an NBFC payment never lands in a bank')

    def test_another_companys_bank_cannot_be_used_or_seen(self):
        theirs = ARBank.objects.create(company=self.other, name='Their Bank')
        self.assertEqual(self.pay(bank=theirs.id).status_code, 400)
        self.assertNotIn(theirs.id, [b['id'] for b in self.api.get('/api/ar/banks/').json()['results']])
        self.assertEqual(self.api.patch(f'/api/ar/banks/{theirs.id}/', {'name': 'x'}, format='json').status_code, 404)

    def test_duplicate_names_are_refused(self):
        self.add_bank('HDFC')
        r = self.api.post('/api/ar/banks/', {'name': ' hdfc ', 'opening_balance': '0'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_a_used_bank_is_retired_not_deleted(self):
        used, unused = self.add_bank('HDFC'), self.add_bank('SBI')
        self.pay(bank=used)
        self.assertEqual(self.api.delete(f'/api/ar/banks/{used}/').json(), {'retired': True})
        self.assertFalse(self.bank(used)['is_active'])
        self.assertEqual(self.api.delete(f'/api/ar/banks/{unused}/').status_code, 204)
        self.assertEqual(self.pay(bank=used).status_code, 400, 'a retired bank takes no new payments')

    def test_bank_details_are_encrypted_at_rest(self):
        bid = self.add_bank('HDFC Secret Branch', '777777')
        with connection.cursor() as cur:
            cur.execute('SELECT name, opening_balance FROM receivables_arbank WHERE id = %s', [bid])
            name, opening = cur.fetchone()
        self.assertNotIn('HDFC', str(name))
        self.assertNotIn('777777', str(opening))

    def test_adding_a_bank_is_logged(self):
        from activity.models import ActivityLog
        self.add_bank('Kotak')
        self.assertTrue(any('Kotak' in (row.summary or '') for row in ActivityLog.objects.all()))

    def test_statement_runs_a_balance_and_rolls_up_before_a_range(self):
        bid = self.add_bank('HDFC', '1000000')
        self.pay(bank=bid, paid_on='2025-08-01', amount='100000')
        self.pay(bank=bid, paid_on='2025-09-12', amount='200000')
        self.pay(bank=bid, paid_on='2025-10-05', amount='300000')
        self.pay(mode='nbfc', paid_on='2025-09-20', amount='999999')   # never in a bank
        st = self.api.get(f'/api/ar/banks/{bid}/statement/').json()
        self.assertEqual([r['balance'] for r in st['rows']], [1100000, 1300000, 1600000])
        self.assertEqual(st['closing_balance'], self.bank(bid)['balance'])
        self.assertEqual(st['rows'][0]['client'], 'Jigar Makwana')
        st = self.api.get(f'/api/ar/banks/{bid}/statement/?from=2025-09-01&to=2025-09-30').json()
        self.assertEqual((st['brought_forward'], st['total_in'], st['closing_balance']), (1100000, 200000, 1300000))
        self.assertEqual(len(st['rows']), 1)

    def test_another_companys_statement_is_hidden(self):
        theirs = ARBank.objects.create(company=self.other, name='Their Bank')
        self.assertEqual(self.api.get(f'/api/ar/banks/{theirs.id}/statement/').status_code, 404)

    def test_bank_master_is_its_own_module(self):
        """Opening Bank Master, its statements and adding banks need the Bank Master
        tick. AR keeps the bank list (Record Payment / refunds pick a bank and show
        its balance) but nothing more; Accounts & Finance alone gets nothing."""
        mk = lambda code, mods: User.objects.create_user(f'{code}@test.local', company=self.co, user_code=code,
                                                         password='x', name=code, role='Employee', modules=mods)
        banker, ar_only, fin_only = mk('BM1', ['Bank Master']), mk('AR2', ['AR']), mk('FN1', ['Accounts & Finance'])
        bid = self.add_bank('HDFC', '0')
        c = APIClient()

        c.force_authenticate(banker)
        self.assertEqual(c.get('/api/ar/banks/').status_code, 200)
        self.assertTrue(c.get('/api/ar/banks/').data['can_manage'])
        self.assertEqual(c.get(f'/api/ar/banks/{bid}/statement/').status_code, 200)
        self.assertEqual(c.post('/api/ar/banks/', {'name': 'SBI', 'opening_balance': '0'}, format='json').status_code, 201)

        c.force_authenticate(ar_only)
        res = c.get('/api/ar/banks/')
        self.assertEqual(res.status_code, 200, 'AR still picks a bank when recording a payment')
        self.assertFalse(res.data['can_manage'])
        self.assertEqual(c.get(f'/api/ar/banks/{bid}/statement/').status_code, 403)
        self.assertEqual(c.post('/api/ar/banks/', {'name': 'Axis', 'opening_balance': '0'}, format='json').status_code, 403)

        c.force_authenticate(fin_only)
        self.assertEqual(c.get('/api/ar/banks/').status_code, 403, 'Accounts & Finance alone does not open banks')
        self.assertEqual(c.get(f'/api/ar/banks/{bid}/statement/').status_code, 403)

