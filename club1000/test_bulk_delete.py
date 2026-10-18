"""Deleting investors goes through the scheme's approvers; what their leads read afterwards."""
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Lead, Payout, Scheme
from companies.models import Company


class BulkDeleteTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='BDL', name='Bulk Co', is_active=True)
        self.admin = User.objects.create_user('a@bdl.com', company=self.co, user_code='BDL1', password='p',
                                              name='Admin', role='Admin', modules=['Club 1000'])
        self.scheme = Scheme.objects.create(company=self.co, name='RISE', tenure_months=12, min_ticket_size=1)
        self.api = APIClient(); self.api.force_authenticate(self.admin)

    def _inv(self, name, phone, **kw):
        lead = Lead.objects.create(company=self.co, name=name, phone=phone, status='converted', scheme_interest=self.scheme)
        return Investor.objects.create(company=self.co, scheme=self.scheme, added_by=self.admin, name=name, phone=phone,
                                       amount_invested=Decimal('100000'), investment_date=date(2025, 1, 1),
                                       maturity_date=date(2025, 1, 1) + relativedelta(months=12), total_return_pct=Decimal('15'),
                                       status='active', approval_status='approved', lead=lead, **kw)

    def test_delete_is_immediate_and_marks_the_lead(self):
        a = self._inv('A', '9800000001')
        lead = a.lead
        self.assertEqual(self.api.delete(f'/api/club1000/investors/{a.pk}/').status_code, 204)
        self.assertFalse(Investor.objects.filter(pk=a.pk).exists())
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'investor_deleted')

    def test_paid_investor_asks_first_then_deletes_its_payouts_too(self):
        c = self._inv('C', '9800000003')
        Payout.objects.create(investor=c, payout_type='interest', due_date=date(2025, 4, 1), amount_due=Decimal('100'),
                              status='paid', paid_amount=Decimal('100'))
        r = self.api.delete(f'/api/club1000/investors/{c.pk}/')
        self.assertEqual((r.status_code, r.json()['code']), (409, 'has_paid'))
        self.assertEqual(self.api.delete(f'/api/club1000/investors/{c.pk}/?include_paid=1').status_code, 204)
        self.assertFalse(Payout.objects.filter(investor_id=c.pk).exists())

    def test_bulk_delete(self):
        a, b, c = self._inv('Aa', '9800000001'), self._inv('Bb', '9800000002'), self._inv('Cc', '9800000003')
        Payout.objects.create(investor=c, payout_type='interest', due_date=date(2025, 4, 1), amount_due=Decimal('100'), status='paid')
        r = self.api.post('/api/club1000/investors/bulk-delete/', {'ids': [a.pk, b.pk, c.pk]}, format='json')
        self.assertEqual(sorted(r.json()['deleted']), sorted([a.pk, b.pk]))
        self.assertEqual([x['id'] for x in r.json()['refused']], [c.pk])
        r = self.api.post('/api/club1000/investors/bulk-delete/', {'ids': [c.pk], 'include_paid': True}, format='json')
        self.assertEqual(r.json()['deleted'], [c.pk])

    def test_only_managers(self):
        u = User.objects.create_user('e@bdl.com', company=self.co, user_code='BDL2', password='p',
                                     name='Emp', role='Employee', modules=['Club 1000'])
        api = APIClient(); api.force_authenticate(u)
        a = self._inv('A', '9800000001')
        self.assertEqual(api.post('/api/club1000/investors/bulk-delete/', {'ids': [a.pk]}, format='json').status_code, 403)
        self.assertEqual(api.delete(f'/api/club1000/investors/{a.pk}/').status_code, 403)
