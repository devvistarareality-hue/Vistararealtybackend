"""A saved draft holds its units until it is deleted, a bare pick expires, and signing
in stamps "Last login"."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from accounts.views import get_tokens_for_user
from companies.models import Company
from sales.models import Booking, Plot, Project
from sales.views import _release_expired_holds


class DraftHoldTimeoutTests(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='AAA', name='Alpha')
        self.rep = User.objects.create(email='rep@x.com', company=self.co, role='STM', user_code='R1')
        self.project = Project.objects.create(company=self.co, name='Kalrav')
        old = timezone.now() - timedelta(hours=2)
        self.plot = Plot.objects.create(project=self.project, number='1', status='hold',
                                        held_by=self.rep, held_at=old, pre_hold_status='available')
        self.draft = Booking.objects.create(company=self.co, project=self.project, plot=self.plot,
                                            plot_ids=[self.plot.id], client_name='X', status='draft',
                                            stm=self.rep)

    def test_recent_draft_keeps_the_hold(self):
        _release_expired_holds(Plot.objects.filter(pk=self.plot.pk))
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'hold')

    def test_an_old_draft_keeps_the_unit_until_it_is_deleted(self):
        # Drafted by an STM stays theirs however old the draft — only deleting it frees the unit.
        Booking.objects.filter(pk=self.draft.pk).update(updated_at=timezone.now() - timedelta(days=60))
        _release_expired_holds(Plot.objects.filter(pk=self.plot.pk))
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'hold')

    def test_a_pick_without_a_draft_still_expires(self):
        Booking.objects.filter(pk=self.draft.pk).delete()
        _release_expired_holds(Plot.objects.filter(pk=self.plot.pk))
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'available')


class LastLoginTests(TestCase):
    def test_sign_in_stamps_last_login_but_view_as_does_not(self):
        co = Company.objects.create(code='AAA', name='Alpha')
        admin = User.objects.create(email='a@x.com', company=co, role='Admin', user_code='A1')
        rep = User.objects.create(email='r@x.com', company=co, role='STM', user_code='R1')
        get_tokens_for_user(rep, impersonator=admin.id)
        rep.refresh_from_db()
        self.assertIsNone(rep.last_login)
        get_tokens_for_user(rep)
        rep.refresh_from_db()
        self.assertIsNotNone(rep.last_login)


class OldNotificationCleanupTests(TestCase):
    def test_only_old_read_notifications_are_deleted(self):
        from accounts.models import Notification
        from sales.management.commands.run_scheduled_notifications import Command, NOTIFICATION_KEEP_DAYS
        co = Company.objects.create(code='AAA', name='Alpha')
        u = User.objects.create(email='u@x.com', company=co, role='STM', user_code='U1')
        old = timezone.now() - timedelta(days=NOTIFICATION_KEEP_DAYS + 1)
        keep_unread = Notification.objects.create(recipient=u, title='old unread')
        drop = Notification.objects.create(recipient=u, title='old read', is_read=True)
        keep_recent = Notification.objects.create(recipient=u, title='new read', is_read=True)
        Notification.objects.filter(pk__in=[keep_unread.pk, drop.pk]).update(created_at=old)
        self.assertEqual(Command()._drop_old_notifications(timezone.now()), 1)
        self.assertEqual(set(Notification.objects.values_list('pk', flat=True)), {keep_unread.pk, keep_recent.pk})
