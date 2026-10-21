"""Paying the principal back before maturity closes the investment once nothing
else is owed — and opens it again if that payment is undone."""
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Payout, Scheme
from companies.models import Company


class CloseEarlyTests(TestCase):
    def setUp(self):
        co = Company.objects.create(code='CLE', name='Close Co', is_active=True)
        admin = User.objects.create_user('a@cle.com', company=co, user_code='CLE1', password='p',
                                         name='Admin', role='Admin', modules=['Club 1000'])
        scheme = Scheme.objects.create(company=co, name='BUY-BACK', tenure_months=24, min_ticket_size=1)
        start = date.today() - relativedelta(days=1)
        self.inv = Investor.objects.create(company=co, scheme=scheme, added_by=admin, name='I', phone='9800000002',
                                           amount_invested=Decimal('10000000'), investment_date=start,
                                           maturity_date=start + relativedelta(months=24), interest_payout='maturity',
                                           status='active', approval_status='approved')
        self.mat = Payout.objects.create(investor=self.inv, payout_type='maturity', due_date=self.inv.maturity_date,
                                         amount_due=Decimal('14000000'), status='pending')
        self.api = APIClient(); self.api.force_authenticate(admin)

    def _pay(self, p):
        r = self.api.post(f'/api/club1000/payouts/{p.pk}/mark-paid/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.inv.refresh_from_db()

    def test_principal_paid_early_closes_it(self):
        self._pay(self.mat)
        self.assertEqual(self.inv.status, 'redeemed')
        self.api.post(f'/api/club1000/payouts/{self.mat.pk}/mark-unpaid/', {}, format='json')
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.status, 'active')

    def test_stays_open_while_something_is_still_owed(self):
        Payout.objects.create(investor=self.inv, payout_type='interest', due_date=self.inv.maturity_date - relativedelta(months=3),
                              amount_due=Decimal('5000'), status='pending')
        self._pay(self.mat)
        self.assertEqual(self.inv.status, 'active')

    def test_history_names_principal_and_interest(self):
        self._pay(self.mat)
        rows = self.api.get(f'/api/club1000/investors/{self.inv.pk}/history/').data
        rows = rows.get('events', rows) if isinstance(rows, dict) else rows
        paid = next(r for r in rows if r.get('kind') == 'paid')
        self.assertEqual(paid['title'], 'Principal + interest paid')
        self.assertIn('₹1,00,00,000 principal + ₹40,00,000 interest', paid['detail'])
