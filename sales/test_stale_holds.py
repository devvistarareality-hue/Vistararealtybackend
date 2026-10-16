"""A saved draft holds its units only while someone is working on it, and signing in
stamps "Last login"."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from accounts.views import get_tokens_for_user
from companies.models import Company
from sales.models import Booking, Plot, Project
from sales.views import DRAFT_HOLD_TIMEOUT, _release_expired_holds


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

    def test_abandoned_draft_frees_the_unit_but_is_kept(self):
        Booking.objects.filter(pk=self.draft.pk).update(
            updated_at=timezone.now() - DRAFT_HOLD_TIMEOUT - timedelta(hours=1))
        _release_expired_holds(Plot.objects.filter(pk=self.plot.pk))
        self.plot.refresh_from_db()
        self.assertEqual(self.plot.status, 'available')
        self.assertIsNone(self.plot.held_by_id)
        self.assertTrue(Booking.objects.filter(pk=self.draft.pk, status='draft').exists())


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
