"""Collections tab, follow-ups and their reminders.

    DATABASE_URL= DB_ENGINE=django.db.backends.sqlite3 ./venv/bin/python manage.py test receivables
"""
from datetime import date, datetime, timedelta, time
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Project
from receivables.models import ARFollowUp
from receivables import reminders
from receivables.test_api import make_booking


class CollectionsTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='VIS', name='Vistara')
        self.project = Project.objects.create(company=self.co, name='Kalrav 2')
        self.user = User.objects.create_user('ar1@test.local', company=self.co, user_code='AR1', password='x',
                                             name='AR User', role='Employee', modules=['AR'])
        self.mgr = User.objects.create_user('arm@test.local', company=self.co, user_code='ARM', password='x',
                                            name='AR Manager', role='Manager', manager_modules=['AR'])
        self.sales = User.objects.create_user('s1@test.local', company=self.co, user_code='S1', password='x',
                                              name='Sales Only', role='Employee', modules=['Sales'])
        today = timezone.localdate()
        # Two lines overdue (one 40 days, one 5), one due in 10 days, one in 60.
        self.booking = make_booking(self.co, self.project, installments=[
            {'no': 1, 'date': (today - timedelta(days=40)).isoformat(), 'amt': 100000},
            {'no': 2, 'date': (today - timedelta(days=5)).isoformat(), 'amt': 200000},
            {'no': 3, 'date': (today + timedelta(days=10)).isoformat(), 'amt': 300000},
            {'no': 4, 'date': (today + timedelta(days=60)).isoformat(), 'amt': 400000},
        ], total_extra=0, final_amount=1000000)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.aid = self.api.get('/api/ar/accounts/').json()['results'][0]['id']

    def test_overdue_view_lists_who_has_not_paid(self):
        d = self.api.get('/api/ar/collections/?view=overdue').json()
        self.assertEqual(d['counts']['overdue_accounts'], 1)
        self.assertEqual(d['counts']['overdue_amount'], 300000)
        row = d['results'][0]
        self.assertEqual(row['days_overdue'], 40)
        self.assertEqual(row['overdue_installments'], 2)
        self.assertEqual(row['next_due']['amount'], 300000)
        self.assertIsNone(row['followup'])
        self.assertEqual(d['counts']['no_followup'], 1)

    def test_upcoming_window(self):
        d = self.api.get('/api/ar/collections/?view=upcoming&days=30').json()
        self.assertEqual(d['results'][0]['upcoming_amount'], 300000)
        d = self.api.get('/api/ar/collections/?view=upcoming&days=90').json()
        self.assertEqual(d['results'][0]['upcoming_amount'], 700000)

    def test_today_window(self):
        today = timezone.localdate()
        self.booking.installments = self.booking.installments + [{'no': 5, 'date': today.isoformat(), 'amt': 50000}]
        self.booking.final_amount = 1050000
        self.booking.save()
        d = self.api.get('/api/ar/collections/?view=upcoming&days=0').json()
        self.assertEqual(d['days'], 0)
        self.assertEqual(d['results'][0]['upcoming_amount'], 50000)
        self.assertEqual(d['results'][0]['next_due']['date'], today.isoformat())

    def test_payment_clears_overdue(self):
        self.api.post(f'/api/ar/accounts/{self.aid}/receipts/',
                      {'paid_on': timezone.localdate().isoformat(), 'amount': 300000, 'mode': 'bank'}, format='json')
        d = self.api.get('/api/ar/collections/?view=overdue').json()
        self.assertEqual(d['results'], [])

    def test_followup_lifecycle_and_assignment_notice(self):
        when = (timezone.localdate() + timedelta(days=1)).isoformat() + 'T11:00'
        with mock.patch('notifications.notify') as n:
            r = self.api.post(f'/api/ar/accounts/{self.aid}/followups/',
                              {'scheduled_at': when, 'channel': 'call', 'note': 'Ask for inst 1', 'assigned_to': self.mgr.id},
                              format='json')
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(n.call_args[0][0].id, self.mgr.id)
        fid = r.json()['id']
        row = self.api.get('/api/ar/collections/').json()['results'][0]
        self.assertEqual(row['followup']['id'], fid)
        # Closing needs an outcome; closing with a next date books the next one.
        self.assertEqual(self.api.patch(f'/api/ar/followups/{fid}/', {'status': 'done'}, format='json').status_code, 400)
        r = self.api.patch(f'/api/ar/followups/{fid}/', {
            'status': 'done', 'outcome': 'Will pay Friday', 'promised_amount': '100000',
            'promised_on': (timezone.localdate() + timedelta(days=3)).isoformat(),
            'next_at': (timezone.localdate() + timedelta(days=4)).isoformat()}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['promised_amount'], 100000)
        self.assertIn('next', r.json())
        hist = self.api.get(f'/api/ar/accounts/{self.aid}/followups/').json()['results']
        self.assertEqual(len(hist), 2)
        row = self.api.get('/api/ar/collections/').json()['results'][0]
        self.assertEqual(row['followups_done'], 1)
        self.assertEqual(row['last_outcome']['text'], 'Will pay Friday')
        # The note is encrypted at rest.
        from django.db import connection
        with connection.cursor() as c:
            c.execute('SELECT outcome FROM receivables_arfollowup WHERE id=%s', [fid])
            self.assertNotIn('Friday', c.fetchone()[0])

    def test_cannot_assign_to_someone_without_ar(self):
        r = self.api.post(f'/api/ar/accounts/{self.aid}/followups/',
                          {'scheduled_at': timezone.localdate().isoformat(), 'assigned_to': self.sales.id}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_access_gated(self):
        c = APIClient()
        c.force_authenticate(self.sales)
        self.assertEqual(c.get('/api/ar/collections/').status_code, 403)
        self.assertEqual(c.get('/api/ar/followups/').status_code, 403)

    def test_due_reminder_then_escalation_once_each(self):
        f = ARFollowUp.objects.create(account_id=self.aid, scheduled_at=timezone.now() - timedelta(hours=30),
                                      assigned_to=self.user, created_by=self.user)
        now = timezone.now()
        with mock.patch('receivables.reminders.notify') as n, mock.patch('receivables.reminders.notify_many') as nm:
            reminders.followup_reminders(now)
            reminders.followup_escalations(now, now - timedelta(hours=24))
            reminders.followup_reminders(now)
            reminders.followup_escalations(now, now - timedelta(hours=24))
        self.assertEqual(n.call_count, 1)
        self.assertEqual(n.call_args[0][0].id, self.user.id)
        self.assertEqual(nm.call_count, 1)
        self.assertIn(self.mgr.id, [u.id for u in nm.call_args[0][0]])

    def test_morning_digest_once_a_day(self):
        morning = timezone.make_aware(datetime.combine(timezone.localdate(), time(10, 0)))
        with mock.patch('receivables.reminders.notify') as n:
            reminders.daily_digest(morning)
        self.assertEqual({c[0][0].id for c in n.call_args_list}, {self.user.id, self.mgr.id})
        self.assertIn('Overdue: 1 accounts', n.call_args_list[0][0][3])
        # Real notifications now exist, so a second run sends nothing.
        from notifications import notify
        for u in (self.user, self.mgr):
            notify(u, 'ar_collections_digest', 't', 'b', {}, push=False)
        with mock.patch('receivables.reminders.notify') as n:
            reminders.daily_digest(morning)
        self.assertEqual(n.call_count, 0)
        night = timezone.make_aware(datetime.combine(timezone.localdate(), time(22, 0)))
        self.assertEqual(reminders.daily_digest(night), 0)

    def test_due_soon_reminder_once_per_installment(self):
        today = timezone.localdate()
        self.booking.installments = self.booking.installments + [
            {'no': 5, 'date': (today + timedelta(days=reminders.DUE_SOON_DAYS)).isoformat(), 'amt': 70000}]
        self.booking.final_amount = 1070000
        self.booking.save()
        morning = timezone.make_aware(datetime.combine(today, time(10, 0)))
        from notifications import notify_many as real_notify_many
        with mock.patch('receivables.reminders.notify_many', side_effect=lambda *a, **k: real_notify_many(*a, **{**k, 'push': False})) as nm:
            self.assertEqual(reminders.due_soon_reminders(morning), 1)
            self.assertEqual(reminders.due_soon_reminders(morning), 0, 'never twice for the same installment')
        # No follow-up owner, so the AR team hears — the AR user and the AR manager.
        self.assertEqual({u.id for u in nm.call_args_list[0][0][0]}, {self.user.id, self.mgr.id})
        self.assertIn('₹70,000', nm.call_args_list[0][0][3])

    def test_receipts_and_followups_are_in_the_activity_log(self):
        self.api.post(f'/api/ar/accounts/{self.aid}/receipts/',
                      {'paid_on': timezone.localdate().isoformat(), 'amount': 5000, 'mode': 'bank'}, format='json')
        from activity.models import ActivityLog
        row = ActivityLog.objects.latest('id')
        self.assertEqual(row.module, 'AR')
        self.assertEqual(row.actor_id, self.user.id)
        self.assertIn('Recorded receipt ₹5,000', row.summary)
        self.assertIn('Jigar Makwana', row.summary)
