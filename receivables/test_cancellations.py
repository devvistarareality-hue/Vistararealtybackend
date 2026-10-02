"""Plot cancellation — run against local SQLite, never the production database:

    DATABASE_URL= DB_ENGINE=django.db.backends.sqlite3 ./venv/bin/python manage.py test receivables
"""
from decimal import Decimal as D

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Closure, Lead, Plot, Project
from receivables.models import ARAccount, ARCancellation
from receivables.test_api import make_booking


class ARCancellationTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='VIS', name='Vistara')
        self.other = Company.objects.create(code='OTH', name='Other')
        self.project = Project.objects.create(company=self.co, name='Kalrav 2')
        self.plot = Plot.objects.create(project=self.project, number='10', status='sold')
        self.admin = User.objects.create_user('adm@test.local', company=self.co, user_code='AD1', password='x',
                                              name='Boss', role='Admin', modules=['AR'])
        self.clerk = User.objects.create_user('ar1@test.local', company=self.co, user_code='AR1', password='x',
                                              name='AR Clerk', role='Employee', modules=['AR'])
        lead = Lead.objects.create(company=self.co, name='Jigar Makwana', phone='8160191851', status='closed')
        self.closure = Closure.objects.create(company=self.co, lead=lead, project=self.project, client_name='Jigar Makwana',
                                              status='booked', closure_date='2025-07-30', unit_no='10', total_amount=16716923)
        # Deal 1,67,16,923, no stamp/registration on this booking -> deal net 1,67,16,923; 10% = 16,71,692.
        self.booking = make_booking(self.co, self.project, plot_id=self.plot.id, plot_ids=[self.plot.id],
                                    closure=self.closure, lead=lead, approval_status='APPROVED')
        self.as_clerk, self.as_admin = APIClient(), APIClient()
        self.as_clerk.force_authenticate(self.clerk)
        self.as_admin.force_authenticate(self.admin)
        self.aid = self.as_clerk.get('/api/ar/accounts/').json()['results'][0]['id']

    def pay(self, amount, bank=None, mode='nbfc', on='2025-08-01'):
        body = {'paid_on': on, 'amount': amount, 'mode': mode, **({'bank': bank} if bank else {})}
        r = self.as_clerk.post(f'/api/ar/accounts/{self.aid}/receipts/', body, format='json')
        self.assertEqual(r.status_code, 201, r.content)

    def bank(self, opening='0'):
        return self.as_admin.post('/api/ar/banks/', {'name': 'HDFC', 'opening_balance': opening}, format='json').json()['id']

    def raise_it(self, reason='Not paying after 6 follow-ups'):
        return self.as_clerk.post(f'/api/ar/accounts/{self.aid}/cancellation/', {'reason': reason}, format='json')

    def test_settlement_keeps_ten_percent_and_refunds_the_rest(self):
        self.pay('2000000')
        prev = self.as_clerk.get(f'/api/ar/accounts/{self.aid}/cancellation/').json()
        self.assertEqual((prev['deal_net'], prev['forfeit'], prev['received'], prev['refund_due']),
                         (16716923, 1671692, 2000000, 328308))

    def test_paid_less_than_ten_percent_means_no_refund_and_nothing_owed(self):
        self.pay('500000')
        prev = self.as_clerk.get(f'/api/ar/accounts/{self.aid}/cancellation/').json()
        self.assertEqual((prev['forfeit'], prev['refund_due']), (500000, 0))

    def test_a_clerk_raises_but_cannot_approve(self):
        r = self.raise_it()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertFalse(r.json()['can_decide'])
        self.assertEqual(self.as_clerk.post(f"/api/ar/cancellations/{r.json()['id']}/decide/", {'action': 'approve'},
                                            format='json').status_code, 403)
        self.assertEqual(self.raise_it().status_code, 400, 'one active cancellation per plot')

    def test_a_reason_is_required(self):
        self.assertEqual(self.raise_it(reason='  ').status_code, 400)

    def test_approval_frees_the_plot_and_freezes_the_account(self):
        cid = self.raise_it().json()['id']
        r = self.as_admin.post(f'/api/ar/cancellations/{cid}/decide/', {'action': 'approve', 'note': 'ok'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.plot.refresh_from_db(); self.booking.refresh_from_db(); self.closure.refresh_from_db()
        self.assertEqual(self.plot.status, 'available', 'the plot is back on sale at once')
        self.assertEqual(self.booking.approval_status, 'CANCELLED')
        self.assertEqual(self.closure.status, 'cancelled')
        self.assertEqual(ARAccount.objects.get(pk=self.aid).status, 'frozen')
        self.assertEqual(self.as_clerk.get('/api/ar/accounts/').json()['results'], [], 'gone from the register')

    def test_rejection_changes_nothing(self):
        cid = self.raise_it().json()['id']
        self.as_admin.post(f'/api/ar/cancellations/{cid}/decide/', {'action': 'reject'}, format='json')
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'sold')
        self.assertEqual(ARCancellation.objects.get(pk=cid).status, 'rejected')
        self.assertEqual(self.raise_it().status_code, 201, 'can be raised again after a rejection')

    def test_refunds_come_out_of_the_bank_in_parts(self):
        bid = self.bank('5000000')
        self.pay('2000000')
        cid = self.raise_it().json()['id']
        self.assertEqual(self.as_clerk.post(f'/api/ar/cancellations/{cid}/refunds/', {
            'paid_on': '2025-09-01', 'amount': '1000', 'bank': bid}, format='json').status_code, 400, 'not before approval')
        self.as_admin.post(f'/api/ar/cancellations/{cid}/decide/', {'action': 'approve'}, format='json')
        r = self.as_clerk.post(f'/api/ar/cancellations/{cid}/refunds/', {
            'paid_on': '2025-09-01', 'amount': '200000', 'bank': bid, 'reference': 'UTR123'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()['refunded'], r.json()['refund_balance'], r.json()['stage']), (200000, 128308, 'refund_pending'))
        over = self.as_clerk.post(f'/api/ar/cancellations/{cid}/refunds/', {
            'paid_on': '2025-09-02', 'amount': '200000', 'bank': bid}, format='json')
        self.assertEqual(over.status_code, 400, 'cannot refund more than is due')
        self.as_clerk.post(f'/api/ar/cancellations/{cid}/refunds/', {'paid_on': '2025-09-02', 'amount': '128308', 'bank': bid}, format='json')
        bank = next(b for b in self.as_clerk.get('/api/ar/banks/').json()['results'] if b['id'] == bid)
        self.assertEqual((bank['paid_out'], bank['balance']), (328308, 5000000 - 328308))
        st = self.as_clerk.get(f'/api/ar/banks/{bid}/statement/').json()
        self.assertEqual([r['kind'] for r in st['rows']], ['out', 'out'])
        self.assertEqual(st['closing_balance'], bank['balance'])
        row = next(c for c in self.as_clerk.get('/api/ar/cancellations/').json()['results'] if c['id'] == cid)
        self.assertEqual(row['stage'], 'refunded')

    def test_deleting_a_refund_puts_the_money_back(self):
        bid = self.bank('0')
        self.pay('2000000')
        cid = self.raise_it().json()['id']
        self.as_admin.post(f'/api/ar/cancellations/{cid}/decide/', {'action': 'approve'}, format='json')
        rid = self.as_clerk.post(f'/api/ar/cancellations/{cid}/refunds/', {
            'paid_on': '2025-09-01', 'amount': '100000', 'bank': bid}, format='json').json()['refunds'][0]['id']
        self.assertEqual(self.as_clerk.delete(f'/api/ar/refunds/{rid}/').status_code, 204)
        bank = next(b for b in self.as_clerk.get('/api/ar/banks/').json()['results'] if b['id'] == bid)
        self.assertEqual(bank['balance'], 0)

    def test_letter_is_issued_after_approval(self):
        self.pay('2000000')
        cid = self.raise_it().json()['id']
        self.assertEqual(self.as_clerk.get(f'/api/ar/cancellations/{cid}/letter/').status_code, 400)
        self.as_admin.post(f'/api/ar/cancellations/{cid}/decide/', {'action': 'approve'}, format='json')
        r = self.as_clerk.get(f'/api/ar/cancellations/{cid}/letter/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for want in ('Cancellation of Booking', 'Jigar Makwana', 'Plot 10', 'Statement of account', 'CAN/'):
            self.assertIn(want, html)

    def test_another_company_sees_nothing(self):
        cid = self.raise_it().json()['id']
        outsider = User.objects.create_user('ot@test.local', company=self.other, user_code='OT1', password='x',
                                            name='O', role='Admin', modules=['AR'])
        c = APIClient(); c.force_authenticate(outsider)
        self.assertEqual(c.get('/api/ar/cancellations/').json()['results'], [])
        self.assertEqual(c.post(f'/api/ar/cancellations/{cid}/decide/', {'action': 'approve'}, format='json').status_code, 404)

    def test_amounts_are_encrypted_at_rest(self):
        self.pay('2000000')
        cid = self.raise_it(reason='Secret reason text').json()['id']
        with connection.cursor() as cur:
            cur.execute('SELECT reason, refund_due FROM receivables_arcancellation WHERE id = %s', [cid])
            reason, refund = cur.fetchone()
        self.assertNotIn('Secret', str(reason))
        self.assertNotIn('328308', str(refund))

    def test_raising_and_approving_are_logged(self):
        from activity.models import ActivityLog
        cid = self.raise_it().json()['id']
        self.as_admin.post(f'/api/ar/cancellations/{cid}/decide/', {'action': 'approve'}, format='json')
        lines = ' | '.join(row.summary or '' for row in ActivityLog.objects.all())
        self.assertIn('Raised cancellation', lines)
        self.assertIn('Approved cancellation', lines)
