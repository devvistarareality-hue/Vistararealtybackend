"""The Accounts & Finance dashboard summary agrees with the four tiles."""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Project


class AccountsSummaryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='ACS', name='Acs Co')
        self.alpha = Project.objects.create(company=self.co, name='Alpha')
        self.beta = Project.objects.create(company=self.co, name='Beta')
        self.admin = User.objects.create_user('a@acs.com', company=self.co, user_code='ACS-A', password='x',
                                              name='Admin', role='Admin')
        now = timezone.now()
        mk = lambda project, amount, acc, status='sold', days=0: Booking.objects.create(
            company=self.co, project=project, client_name='C', final_amount=amount, status=status,
            accounts_status=acc, approved_at=now - timedelta(days=days),
            accounts_approved_at=now if acc == 'approved' else None)
        mk(self.alpha, 100, 'pending', days=10)
        mk(self.alpha, 200, 'pending', days=2)
        mk(self.alpha, 300, 'approved')
        mk(self.beta, 400, 'approved')
        mk(self.beta, 500, 'rejected')
        mk(self.beta, 999, 'approved', status='pending')      # not at Accounts yet

    def test_summary_matches_the_counts_and_adds_up(self):
        c = APIClient(); c.force_authenticate(self.admin)
        counts = c.get('/api/sales/bookings/all/?counts_only=true').json()
        d = c.get('/api/sales/bookings/all/?summary=true').json()
        for k in ('pending', 'approved', 'rejected', 'total'):
            self.assertEqual(d[k], counts[k], k)
        self.assertEqual(d['total'], 5)
        self.assertEqual(d['values'], {'pending': 300.0, 'approved': 700.0, 'rejected': 500.0})
        alpha = next(r for r in d['by_project'] if r['name'] == 'Alpha')
        self.assertEqual((alpha['pending'], alpha['approved'], alpha['approved_value']), (2, 1, 300.0))
        # Oldest first.
        self.assertEqual([w['days'] for w in d['oldest_pending']], [10, 2])
        self.assertEqual(len(d['trend']), 6)
        self.assertEqual(d['trend'][-1]['count'], 2)
        self.assertEqual(d['trend'][-1]['value'], 700.0)
