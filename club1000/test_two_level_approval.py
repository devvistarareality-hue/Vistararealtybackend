"""Two-level investor approval: level 1 passes it on, level 2 makes it final."""
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from club1000.models import Investor, Scheme
from companies.models import Company


class TwoLevelApprovalTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='TLA', name='Two Co', is_active=True)
        mk = lambda e, c, n: User.objects.create_user(e, company=self.co, user_code=c, password='p', name=n, role='Manager', modules=['Club 1000'], manager_modules=['Club 1000'])
        self.admin = User.objects.create_user('ad@tla.com', company=self.co, user_code='TLA0', password='p', name='Admin', role='Admin', modules=['Club 1000'])
        self.l1, self.l2 = mk('l1@tla.com', 'TLA1', 'Level One'), mk('l2@tla.com', 'TLA2', 'Level Two')
        self.scheme = Scheme.objects.create(company=self.co, name='RISE', tenure_months=12, min_ticket_size=1,
                                            investor_approvers=[self.l1.id], investor_approvers_l2=[self.l2.id])
        self.inv = Investor.objects.create(company=self.co, scheme=self.scheme, added_by=self.admin, name='X', phone='9800000001',
                                           amount_invested=Decimal('100000'), investment_date=date.today(),
                                           maturity_date=date.today() + relativedelta(months=12), total_return_pct=Decimal('15'),
                                           interest_payout='maturity', approval_status='pending')

    def _api(self, u):
        a = APIClient(); a.force_authenticate(u); return a

    def _approve(self, u):
        return self._api(u).post(f'/api/club1000/investors/{self.inv.pk}/action/', {'action': 'approve'}, format='json')

    def test_level2_cannot_skip_level1(self):
        self.assertEqual(self._approve(self.l2).status_code, 403)

    def test_level1_then_level2_is_final(self):
        r = self._approve(self.l1)
        self.assertEqual((r.status_code, r.data.get('level')), (200, 1))
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.approval_status, self.inv.approval_step['by_name']), ('pending', 'Level One'))
        self.assertEqual(self._approve(self.l1).status_code, 403)                 # level 1 cannot finish it
        r = self._approve(self.l2)
        self.assertEqual(r.status_code, 200, r.data)
        self.inv.refresh_from_db()
        self.assertEqual((self.inv.approval_status, self.inv.approval_step), ('approved', None))
        self.assertTrue(self.inv.payouts.exists())

    def test_single_level_when_no_level2(self):
        Scheme.objects.filter(pk=self.scheme.pk).update(investor_approvers_l2=None)
        self.assertEqual(self._approve(self.l1).status_code, 200)
        self.inv.refresh_from_db()
        self.assertEqual(self.inv.approval_status, 'approved')

    def test_level2_list_is_configured(self):
        r = self._api(self.admin).post(f'/api/club1000/schemes/{self.scheme.pk}/toggle-approver/', {'manager_id': self.l1.id, 'level': 2}, format='json')
        self.assertEqual(sorted(r.data['investor_approvers_l2']), sorted([self.l2.id, self.l1.id]))
