"""Notification settings per designation / person: what is sent, pushed, or summarised."""
from datetime import timedelta
from unittest import mock

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import Designation, Notification, User
from companies.models import Company
from notifications import notify, notify_team
from sales.tests import auth


class NotificationPrefsTests(APITestCase):
    def setUp(self):
        self.co = Company.objects.create(name='N Co', code='NCO')
        self.md = User.objects.create(email='md@n.com', company=self.co, role='Director', user_code='N1', designation='MD')
        self.head = User.objects.create(email='h@n.com', company=self.co, role='Manager', user_code='N2',
                                        designation='Cluster Head', reporting_manager=self.md)
        self.rep = User.objects.create(email='r@n.com', company=self.co, role='Employee', user_code='N3',
                                       designation='Telecaller', reporting_manager=self.head)
        self.admin = User.objects.create(email='a@n.com', company=self.co, role='Admin', user_code='N4')

    def prefs(self, name, prefs):
        Designation.objects.create(company=self.co, name=name, module='Sales', notification_prefs=prefs)

    @mock.patch('notifications.send_push_to_user')
    def test_default_sends_and_pushes(self, push):
        notify(self.rep, 'new_lead', 'New Lead Assigned', 'x')
        self.assertEqual(Notification.objects.filter(recipient=self.rep).count(), 1)
        self.assertEqual(push.call_count, 1)

    @mock.patch('notifications.send_push_to_user')
    def test_off_and_inapp(self, push):
        self.prefs('Telecaller', {'new_lead': {'mode': 'off'}, 'booking_result': {'mode': 'inapp'}})
        notify(self.rep, 'new_lead', 'New Lead Assigned', 'x')
        notify(self.rep, 'booking_approved', 'Booking approved', 'x')   # type → booking_result kind
        self.assertEqual(list(Notification.objects.filter(recipient=self.rep).values_list('type', flat=True)),
                         ['booking_approved'])
        self.assertEqual(push.call_count, 0)

    @mock.patch('notifications.send_push_to_user')
    def test_person_override_beats_designation(self, push):
        self.prefs('Telecaller', {'new_lead': {'mode': 'off'}})
        self.rep.notification_overrides = {'new_lead': {'mode': 'push'}}
        self.rep.save()
        notify(User.objects.get(pk=self.rep.pk), 'new_lead', 'New Lead Assigned', 'x')
        self.assertEqual(Notification.objects.filter(recipient=self.rep).count(), 1)

    @mock.patch('notifications.send_push_to_user')
    def test_team_levels_and_digest(self, push):
        # Before: both the cluster head (level 1) and the MD (level 2) were told.
        notify_team(self.rep, 'followup_overdue', 'Team follow-up overdue', 'x', kind='team_followup_overdue')
        self.assertEqual(Notification.objects.filter(title='Team follow-up overdue').count(), 2)
        Notification.objects.all().delete()
        # MD: only direct reports' alerts; Cluster Head: morning summary instead.
        self.prefs('MD', {'team_followup_overdue': {'mode': 'push', 'levels': 1, 'digest': False}})
        self.prefs('Cluster Head', {'team_followup_overdue': {'mode': 'push', 'levels': 5, 'digest': True}})
        notify_team(User.objects.get(pk=self.rep.pk), 'followup_overdue', 'Team follow-up overdue', 'x',
                    kind='team_followup_overdue')
        self.assertEqual(Notification.objects.count(), 0)
        # The rep's own reminder is a different kind and still goes out.
        notify(User.objects.get(pk=self.rep.pk), 'followup_overdue', 'Follow-up overdue', 'x')
        self.assertEqual(Notification.objects.filter(recipient=self.rep).count(), 1)

    @mock.patch('notifications.send_push_to_user')
    def test_morning_summary(self, push):
        from sales.management.commands.run_scheduled_notifications import Command
        from sales.models import FollowUp, Lead
        self.prefs('Cluster Head', {'team_followup_overdue': {'mode': 'push', 'levels': 1, 'digest': True}})
        lead = Lead.objects.create(company=self.co, name='L', phone='+919000000009', telecaller=self.rep)
        past = timezone.now() - timedelta(days=2)
        for _ in range(3):
            FollowUp.objects.create(lead=lead, assigned_to=self.rep, scheduled_at=past, status='pending')
        nine = timezone.localtime().replace(hour=10, minute=0)
        self.assertEqual(Command()._team_digests(nine, dry=False), 1)
        n = Notification.objects.get(recipient=self.head, type='team_digest')
        self.assertIn('3 follow-ups in your team are overdue', n.body)
        self.assertEqual(Command()._team_digests(nine, dry=False), 0)   # once a day

    def test_designation_patch_cleans(self):
        d = Designation.objects.create(company=self.co, name='STM', module='Sales')
        auth(self.client, self.admin)
        r = self.client.patch(f'/api/auth/designations/{d.id}/', {'notification_prefs': {
            'team_followup_overdue': {'mode': 'push', 'levels': 9, 'digest': 1},
            'new_lead': {'mode': 'loud'}, 'bogus': {'mode': 'off'}}}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Designation.objects.get(pk=d.pk).notification_prefs, {
            'team_followup_overdue': {'mode': 'push', 'levels': 5, 'digest': True},
            'new_lead': {'mode': 'push'}})
        cat = self.client.get('/api/auth/designations/capabilities/').json()
        self.assertTrue(any(k['key'] == 'team_followup_overdue' and k['team'] for k in cat['notifications']))

    def test_person_endpoint_admin_only(self):
        auth(self.client, self.rep)
        self.assertEqual(self.client.patch(f'/api/auth/users/{self.rep.id}/notifications/',
                                           {'notification_overrides': {}}, format='json').status_code, 404)
        auth(self.client, self.admin)
        r = self.client.patch(f'/api/auth/users/{self.rep.id}/notifications/',
                              {'notification_overrides': {'task': {'mode': 'inapp'}}}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(User.objects.get(pk=self.rep.pk).notification_overrides, {'task': {'mode': 'inapp'}})

    def test_bell_list_says_which_module(self):
        for t in ('new_lead', 'investor_approval', 'accounts_booking_approval', 'ar_due_soon', 'test'):
            Notification.objects.create(recipient=self.rep, type=t, title=t)
        auth(self.client, self.rep)
        got = {r['type']: r['module'] for r in self.client.get('/api/auth/notifications/').json()['results']}
        self.assertEqual(got, {'new_lead': 'Sales', 'investor_approval': 'Club 1000',
                               'accounts_booking_approval': 'Accounts & Finance', 'ar_due_soon': 'Accounts Receivable', 'test': 'Other'})
