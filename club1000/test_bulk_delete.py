"""Deleting several investors at once, and what their leads read afterwards."""
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

    def test_deletes_the_free_ones_and_lists_the_paid_one(self):
        a, b, c = self._inv('A', '9800000001'), self._inv('B', '9800000002'), self._inv('C', '9800000003')
        Payout.objects.create(investor=c, payout_type='interest', due_date=date(2025, 4, 1), amount_due=Decimal('100'), status='paid')
        lead_a = a.lead
        r = self.api.post('/api/club1000/investors/bulk-delete/', {'ids': [a.pk, b.pk, c.pk]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(sorted(r.data['deleted']), sorted([a.pk, b.pk]))
        self.assertEqual([x['id'] for x in r.data['refused']], [c.pk])
        self.assertIn('already paid', r.data['refused'][0]['reason'])
        self.assertEqual(list(Investor.objects.values_list('pk', flat=True)), [c.pk])
        lead_a.refresh_from_db()
        self.assertEqual(lead_a.status, 'investor_deleted')
        self.assertTrue(lead_a.history.filter(new_value='investor_deleted').exists())

    def test_single_delete_marks_the_lead_too(self):
        a = self._inv('A', '9800000001')
        lead = a.lead
        self.assertEqual(self.api.delete(f'/api/club1000/investors/{a.pk}/').status_code, 204)
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'investor_deleted')

    def test_only_managers(self):
        u = User.objects.create_user('e@bdl.com', company=self.co, user_code='BDL2', password='p',
                                     name='Emp', role='Employee', modules=['Club 1000'])
        api = APIClient(); api.force_authenticate(u)
        a = self._inv('A', '9800000001')
        self.assertEqual(api.post('/api/club1000/investors/bulk-delete/', {'ids': [a.pk]}, format='json').status_code, 403)
        self.assertTrue(Investor.objects.filter(pk=a.pk).exists())

    def test_paid_investor_asks_first_then_deletes_its_payouts_too(self):
        from activity.models import ActivityLog
        c = self._inv('C', '9800000003')
        Payout.objects.create(investor=c, payout_type='interest', due_date=date(2025, 4, 1), amount_due=Decimal('100'),
                              status='paid', paid_amount=Decimal('100'))
        Payout.objects.create(investor=c, payout_type='maturity', due_date=date(2026, 1, 1), amount_due=Decimal('100000'))
        r = self.api.delete(f'/api/club1000/investors/{c.pk}/')
        self.assertEqual((r.status_code, r.json()['code'], r.json()['paid_payouts']), (409, 'has_paid', 1))
        self.assertTrue(Investor.objects.filter(pk=c.pk).exists())                 # nothing gone without the OK
        r = self.api.delete(f'/api/club1000/investors/{c.pk}/?include_paid=1')
        self.assertEqual(r.status_code, 204)
        self.assertFalse(Investor.objects.filter(pk=c.pk).exists())
        self.assertFalse(Payout.objects.filter(investor_id=c.pk).exists())
        log = ActivityLog.objects.filter(action='deleted', target_id=str(c.pk)).first()
        self.assertIn('with 1 paid payout (₹100) deleted too', log.summary)

    def test_bulk_delete_logs_everyone(self):
        from activity.models import ActivityLog
        a, b, c = self._inv('Aa', '9800000001'), self._inv('Bb', '9800000002'), self._inv('Cc', '9800000003')
        Payout.objects.create(investor=c, payout_type='interest', due_date=date(2025, 4, 1), amount_due=Decimal('100'), status='paid')
        self.api.post('/api/club1000/investors/bulk-delete/', {'ids': [a.pk, b.pk, c.pk]}, format='json')
        log = ActivityLog.objects.filter(module='Club 1000', path__icontains='bulk-delete').latest('created_at')
        self.assertIn('Deleted 2 investors — Aa, Bb', log.summary)
        self.assertIn('kept 1', log.summary)
        r = self.api.post('/api/club1000/investors/bulk-delete/', {'ids': [c.pk], 'include_paid': True}, format='json')
        self.assertEqual(r.json()['deleted'], [c.pk])
