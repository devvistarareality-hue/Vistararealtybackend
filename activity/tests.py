from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Project
from activity.models import ActivityLog
from activity.recorder import describe


class DescribeTests(TestCase):
    def test_urls_become_readable_lines(self):
        self.assertEqual(describe('POST', '/api/sales/bookings/12/action/', {'action': 'approve'})[4], 'Approved booking #12')
        m = describe('POST', '/api/sales/bookings/12/accounts-action/', {'action': 'reject'})
        self.assertEqual((m[0], m[4]), ('Accounts & Finance', 'Rejected booking #12'))
        self.assertEqual(describe('PATCH', '/api/sales/leads/5/', {'status': 'x', 'remarks': 'y'})[4],
                         'Updated lead #5 (status, remarks)')
        self.assertEqual(describe('DELETE', '/api/sales/follow-ups/9/', None)[4], 'Deleted follow up #9')
        self.assertEqual(describe('POST', '/api/sales/leads/', {}, 77)[4], 'Created lead #77')
        self.assertEqual(describe('POST', '/api/attendance/sign-in/', {})[4], 'Sign in')
        self.assertEqual(describe('POST', '/api/sales/closures/3/cancel/', {})[4], 'Cancelled closure #3')


class ActivityLogTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='ACT', name='Act')
        self.other = Company.objects.create(code='OTH', name='Other')
        self.admin = User.objects.create_user('a@x.com', company=self.co, user_code='A1', password='x', name='Boss', role='Admin')
        self.emp = User.objects.create_user('e@x.com', company=self.co, user_code='E1', password='x', name='Emp', role='Manager')
        self.api = APIClient()

    def test_changes_are_recorded_with_who_and_secrets_are_not(self):
        self.api.force_authenticate(self.admin)
        r = self.api.post('/api/sales/projects/', {'name': 'Kalrav 9', 'password': 'hunter2'}, format='json')
        self.assertLess(r.status_code, 400, r.content)
        row = ActivityLog.objects.latest('id')
        self.assertEqual((row.actor_id, row.module, row.action), (self.admin.id, 'Sales', 'created'))
        self.assertEqual(row.target_id, str(r.json()['id']))
        self.assertNotIn('password', row.details)
        self.assertNotIn('hunter2', row.details)

    def test_reads_and_failures_are_not_logged(self):
        self.api.force_authenticate(self.admin)
        self.api.get('/api/sales/projects/')
        self.api.post('/api/sales/closures/999999/cancel/', {}, format='json')
        self.assertFalse(ActivityLog.objects.exists())

    def test_visibility(self):
        ActivityLog.objects.create(company=self.co, actor=self.admin, actor_name='Boss', module='Sales', action='created',
                                   target_type='booking', target_id='5', summary='Created booking')
        ActivityLog.objects.create(company=self.co, actor=self.emp, actor_name='Emp', module='Sales', action='updated',
                                   target_type='lead', target_id='1', summary='Updated lead')
        ActivityLog.objects.create(company=self.other, module='Sales', action='created', summary='Elsewhere')
        self.api.force_authenticate(self.admin)
        d = self.api.get('/api/activity/').json()
        self.assertEqual(len(d['results']), 2)
        self.assertTrue(d['can_see_all'])
        self.api.force_authenticate(self.emp)
        d = self.api.get('/api/activity/').json()
        self.assertEqual([x['summary'] for x in d['results']], ['Updated lead'])
        # A record's history is open to its company.
        d = self.api.get('/api/activity/?target_type=booking&target_id=5').json()
        self.assertEqual([x['actor']['name'] for x in d['results']], ['Boss'])
        self.assertEqual(len(self.api.get('/api/activity/?q=booking').json()['results']), 0)
