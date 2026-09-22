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
        self.assertEqual(describe('PATCH', '/api/sales/channel-partners/4/', {})[0], 'Channel Partner')


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
        # A module's Log tab can ask for several module names at once.
        self.api.force_authenticate(self.admin)
        ActivityLog.objects.create(company=self.co, actor=self.admin, actor_name='Boss', module='Channel Partner',
                                   action='created', summary='CP thing')
        d = self.api.get('/api/activity/?module=Sales,Channel Partner').json()
        self.assertEqual(len(d['results']), 3)
        self.assertEqual(len(self.api.get('/api/activity/?module=Channel Partner').json()['results']), 1)


class BookingHistoryTests(TestCase):
    def test_history_is_filled_from_the_booking_for_steps_before_the_log(self):
        from django.utils import timezone
        from sales.models import Booking
        co = Company.objects.create(code='BH', name='BH')
        admin = User.objects.create_user('bh@x.com', company=co, user_code='B1', password='x', name='Boss', role='Admin')
        acc = User.objects.create_user('bh2@x.com', company=co, user_code='B2', password='x', name='Acc', role='Manager')
        p = Project.objects.create(company=co, name='P')
        b = Booking.objects.create(company=co, project=p, stm=admin, status='sold', client_name='C', phone='9000000001',
                                   approved_by=admin, approved_at=timezone.now(),
                                   accounts_approved_by=acc, accounts_approved_at=timezone.now())
        api = APIClient()
        api.force_authenticate(admin)
        rows = api.get(f'/api/activity/?target_type=booking&target_id={b.id}').json()['results']
        self.assertEqual({(r['summary'], r['actor']['name']) for r in rows},
                         {('Submitted booking', 'Boss'), ('Approved booking', 'Boss'), ('Approved (Accounts) booking', 'Acc')})
        # Once the log has the Sales approval, the booking field is not repeated.
        ActivityLog.objects.create(company=co, actor=admin, actor_name='Boss', module='Sales', action='approved',
                                   target_type='booking', target_id=str(b.id), summary='Approved booking — C')
        rows = api.get(f'/api/activity/?target_type=booking&target_id={b.id}').json()['results']
        self.assertEqual(sum(1 for r in rows if r['action'] == 'approved' and r['module'] == 'Sales'), 1)


class ChangeCaptureTests(TestCase):
    def setUp(self):
        from sales.models import Lead
        self.co = Company.objects.create(code='CC', name='CC')
        self.admin = User.objects.create_user('cc@x.com', company=self.co, user_code='CC1', password='x', name='Boss', role='Admin')
        self.lead = Lead.objects.create(company=self.co, name='Rahul Shah', phone='9825012345', status='new')
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def test_lead_edit_logs_what_changed_and_names_the_lead(self):
        r = self.api.patch(f'/api/sales/leads/{self.lead.id}/', {
            'name': 'Rahul Shah', 'phone': '9825012345', 'city': 'Vadodara', 'status': 'new'}, format='json')
        self.assertLess(r.status_code, 400, r.content)
        row = ActivityLog.objects.latest('id')
        self.assertIn('Rahul Shah (9825012345)', row.summary)
        self.assertIn('City: — → Vadodara', row.summary)
        self.assertNotIn('Name', row.summary, 'unchanged fields are not listed')
        from django.db import connection
        with connection.cursor() as c:
            c.execute('SELECT details, summary FROM activity_activitylog WHERE id=%s', [row.id])
            raw = ' '.join(c.fetchone())
        self.assertNotIn('Rahul', raw)

    def test_saving_without_changes_writes_no_line(self):
        self.api.patch(f'/api/sales/leads/{self.lead.id}/', {'name': 'Rahul Shah', 'phone': '9825012345'}, format='json')
        self.assertFalse(ActivityLog.objects.exists())

    def test_old_rows_get_the_lead_name(self):
        ActivityLog.objects.create(company=self.co, actor=self.admin, actor_name='Boss', module='Sales', action='updated',
                                   target_type='lead', target_id=str(self.lead.id), summary=f'Updated lead #{self.lead.id}')
        d = self.api.get('/api/activity/').json()
        self.assertEqual(d['results'][0]['label'], 'Rahul Shah (9825012345)')
        self.assertEqual(d['results'][0]['lead_id'], self.lead.id)
        # Everyone in the company can be picked, not only people already in the log.
        User.objects.create_user('cc2@x.com', company=self.co, user_code='CC2', password='x', name='Quiet Rep', role='Employee')
        d = self.api.get('/api/activity/').json()
        self.assertEqual([a['name'] for a in d['actors']], ['Boss', 'Quiet Rep'])


class PlotActionTests(TestCase):
    def test_plot_hold_names_project_and_plot(self):
        from sales.models import Plot
        co = Company.objects.create(code='PH', name='PH')
        admin = User.objects.create_user('ph@x.com', company=co, user_code='PH1', password='x', name='Boss', role='Admin')
        proj = Project.objects.create(company=co, name='Kalrav 2')
        p1 = Plot.objects.create(project=proj, number='12', status='available')
        p2 = Plot.objects.create(project=proj, number='13', status='available')
        api = APIClient()
        api.force_authenticate(admin)
        r = api.post('/api/sales/plots/hold/', {'plot_ids': [p1.id, p2.id]}, format='json')
        self.assertLess(r.status_code, 400, r.content)
        row = ActivityLog.objects.latest('id')
        self.assertEqual(row.summary, 'Held Kalrav 2 Plot 12, 13')
        self.assertEqual(row.target_type, 'plot')
