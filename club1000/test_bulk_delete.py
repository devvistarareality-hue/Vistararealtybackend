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

    def _approve(self, inv):
        return self.api.post(f'/api/club1000/investors/{inv.pk}/pending-action/', {'action': 'approve'}, format='json')

    def test_delete_is_a_request_until_approved(self):
        a = self._inv('A', '9800000001')
        lead = a.lead
        self.assertEqual(self.api.delete(f'/api/club1000/investors/{a.pk}/').status_code, 400)          # reason needed
        r = self.api.delete(f'/api/club1000/investors/{a.pk}/?reason=duplicate')
        self.assertEqual(r.status_code, 202, r.content)
        a.refresh_from_db()
        self.assertEqual(a.pending_action['type'], 'delete')
        pending = self.api.get('/api/club1000/investors/?approval_status=pending').data
        pending = pending if isinstance(pending, list) else pending.get('results', [])
        self.assertIn(a.pk, [x['id'] for x in pending])                                                  # on Approvals
        self.assertEqual(self._approve(a).status_code, 200)
        self.assertFalse(Investor.objects.filter(pk=a.pk).exists())
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'investor_deleted')

    def test_rejected_delete_keeps_the_investor(self):
        a = self._inv('A', '9800000001')
        self.api.delete(f'/api/club1000/investors/{a.pk}/?reason=dup')
        r = self.api.post(f'/api/club1000/investors/{a.pk}/pending-action/', {'action': 'reject', 'reason': 'not a duplicate'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        a.refresh_from_db()
        self.assertIsNone(a.pending_action)
        self.assertEqual(a.rejections[-1]['kind'], 'delete')

    def test_paid_investor_asks_first_then_requests_with_payments(self):
        c = self._inv('C', '9800000003')
        Payout.objects.create(investor=c, payout_type='interest', due_date=date(2025, 4, 1), amount_due=Decimal('100'),
                              status='paid', paid_amount=Decimal('100'))
        r = self.api.delete(f'/api/club1000/investors/{c.pk}/?reason=x')
        self.assertEqual((r.status_code, r.json()['code']), (409, 'has_paid'))
        self.assertEqual(self.api.delete(f'/api/club1000/investors/{c.pk}/?reason=x&include_paid=1').status_code, 202)
        self._approve(c)
        self.assertFalse(Investor.objects.filter(pk=c.pk).exists())
        self.assertFalse(Payout.objects.filter(investor_id=c.pk).exists())

    def test_bulk_delete_files_requests(self):
        a, b = self._inv('Aa', '9800000001'), self._inv('Bb', '9800000002')
        r = self.api.post('/api/club1000/investors/bulk-delete/', {'ids': [a.pk, b.pk], 'reason': 'test data'}, format='json')
        self.assertEqual(sorted(r.json()['requested']), sorted([a.pk, b.pk]))
        self.assertEqual(Investor.objects.filter(pending_action__isnull=False).count(), 2)

    def test_only_managers(self):
        u = User.objects.create_user('e@bdl.com', company=self.co, user_code='BDL2', password='p',
                                     name='Emp', role='Employee', modules=['Club 1000'])
        api = APIClient(); api.force_authenticate(u)
        a = self._inv('A', '9800000001')
        self.assertEqual(api.post('/api/club1000/investors/bulk-delete/', {'ids': [a.pk], 'reason': 'x'}, format='json').status_code, 403)
