"""Directors hear about every booking, in every project.

The other audiences are earned per project: you are told because you approve
that project, or you sold the unit, or Accounts named you on it. A Director is
told regardless — the point is company-wide visibility of what is being sold.

Two things that look like details and are not:

1. These are informational, never approval requests. _notify_booking_approvers
   is scoped to people the approve/reject endpoint will actually accept; a
   Director who is not an approver on that project would tap an approval prompt
   and get a 403.
2. "Everyone is told once" has to hold across both functions, not just within
   one. On submit the approvers are notified by _notify_booking_approvers and
   the rest by _notify_booking_event, so a Director who approves this project
   has to be excluded from the second — otherwise they get the same booking
   twice, once actionable and once not.
"""
from unittest import mock

from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Plot, Project
from sales.views import _notify_booking_approvers, _notify_booking_event


class DirectorBookingNotificationTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DBN', name='Director Notify Co')
        cls.other_co = Company.objects.create(code='DBX', name='Other Co')

        def mk(code, name, role='Employee', company=None, **kw):
            return User.objects.create(
                email=f'{code}@dbn.com', company=company or cls.co, role=role,
                designation=role, user_code=code, name=name, **kw)

        cls.admin = mk('D0', 'Admin', 'Admin')
        cls.sales_mgr = mk('D1', 'Sales Mgr', 'Manager')
        cls.stm = mk('D2', 'Rep', reporting_manager=cls.sales_mgr)
        cls.director_a = mk('D3', 'Director A', 'Director')
        cls.director_b = mk('D4', 'Director B', 'Director')
        # A Director who is also an approver on one project — the double-notify case.
        cls.director_approver = mk('D5', 'Director Approver', 'Director')
        cls.retired = mk('D6', 'Retired Director', 'Director', is_active=False)
        cls.foreign_director = mk('D7', 'Other Co Director', 'Director', company=cls.other_co)

        # Two projects. Neither names director_a or director_b as an approver —
        # that is the whole point: they hear about both regardless.
        cls.project = Project.objects.create(
            company=cls.co, name='Kalrav', booking_approvers=[cls.sales_mgr.id])
        cls.other_project = Project.objects.create(
            company=cls.co, name='Pratishtha', booking_approvers=[cls.director_approver.id])

    def setUp(self):
        cache.clear()
        self.plot = Plot.objects.create(project=self.project, number='12', status='hold')
        self.booking = Booking.objects.create(
            company=self.co, project=self.project, plot=self.plot, plot_ids=[self.plot.id],
            stm=self.stm, status='pending', client_name='Asha', phone='9000000300',
            final_amount=2500000)

    def _sent(self, fn):
        sent = []
        with mock.patch('notifications.notify',
                        side_effect=lambda u, ntype, title, *a, **k: sent.append((u.id, ntype, title))):
            fn()
        return sent

    def _event(self, event, actor=None, booking=None, skip_ids=None):
        return self._sent(lambda: _notify_booking_event(
            self.co, booking or self.booking, event, actor, skip_ids=skip_ids))

    # ------------------------------------------------- every booking, every project

    def test_a_director_is_told_about_a_project_they_do_not_approve(self):
        ids = [s[0] for s in self._event('sales_approved', actor=self.admin)]
        self.assertIn(self.director_a.id, ids)
        self.assertIn(self.director_b.id, ids)

    def test_a_director_is_told_about_every_project(self):
        other_plot = Plot.objects.create(project=self.other_project, number='7', status='hold')
        other = Booking.objects.create(
            company=self.co, project=self.other_project, plot=other_plot,
            plot_ids=[other_plot.id], stm=self.stm, status='pending',
            client_name='Bhavin', phone='9000000301', final_amount=1800000)
        ids = [s[0] for s in self._event('sales_approved', actor=self.admin, booking=other)]
        self.assertIn(self.director_a.id, ids)
        self.assertIn(self.director_b.id, ids)

    def test_every_event_reaches_the_directors(self):
        for event in ('submitted', 'sales_approved', 'sales_rejected',
                      'accounts_approved', 'accounts_rejected', 'cancelled'):
            ids = [s[0] for s in self._event(event, actor=self.admin)]
            self.assertIn(self.director_a.id, ids, f'{event} skipped the directors')

    # --------------------------------------------------------- informational only

    def test_directors_never_get_an_approval_request(self):
        # An approval prompt to a Director who is not an approver on this project
        # 403s the moment they tap it. Theirs must be the informational type.
        for event in ('submitted', 'sales_approved', 'cancelled'):
            for uid, ntype, _title in self._event(event, actor=self.admin):
                if uid == self.director_a.id:
                    self.assertNotIn('approval', ntype,
                                     f'{event} sent director an actionable {ntype}')

    def test_the_approval_request_itself_still_skips_non_approver_directors(self):
        ids = [s[0] for s in self._sent(
            lambda: _notify_booking_approvers(self.co, self.booking, self.stm))]
        self.assertIn(self.sales_mgr.id, ids)
        self.assertNotIn(self.director_a.id, ids,
                         'not an approver here — would 403 on tap')

    # ------------------------------------------------------------- told only once

    def test_a_director_who_approves_this_project_is_not_told_twice(self):
        other_plot = Plot.objects.create(project=self.other_project, number='8', status='hold')
        other = Booking.objects.create(
            company=self.co, project=self.other_project, plot=other_plot,
            plot_ids=[other_plot.id], stm=self.stm, status='pending',
            client_name='Chirag', phone='9000000302', final_amount=1900000)

        # The real submit path: approvers first, then everyone else, the second
        # call told whom the first already reached.
        asked = {}
        first = self._sent(lambda: asked.update(
            ids=_notify_booking_approvers(self.co, other, self.stm)))
        rest = self._event('submitted', actor=self.stm, booking=other, skip_ids=asked['ids'])

        approver_hits = [s for s in first + rest if s[0] == self.director_approver.id]
        self.assertEqual(len(approver_hits), 1,
                         f'told {len(approver_hits)} times: {approver_hits}')
        # And the one they got is the actionable one, since they can act.
        self.assertIn('approval', approver_hits[0][1])

    def test_a_director_is_not_told_about_their_own_action(self):
        ids = [s[0] for s in self._event('sales_approved', actor=self.director_a)]
        self.assertNotIn(self.director_a.id, ids)
        self.assertIn(self.director_b.id, ids, 'the others still hear about it')

    def test_a_director_who_sold_the_unit_keeps_the_reps_own_notice(self):
        # director_a as the rep: 'owner' is checked before 'directors', so they get
        # the receipt rather than the generic company-wide line.
        self.booking.stm = self.director_a
        self.booking.save(update_fields=['stm'])
        hits = [s for s in self._event('sales_approved', actor=self.admin)
                if s[0] == self.director_a.id]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], 'booking_approved', 'should be the rep receipt')

    # ------------------------------------------------------------------ scoping

    def test_another_companys_directors_are_never_told(self):
        ids = [s[0] for s in self._event('sales_approved', actor=self.admin)]
        self.assertNotIn(self.foreign_director.id, ids)

    def test_a_deactivated_director_is_not_told(self):
        ids = [s[0] for s in self._event('sales_approved', actor=self.admin)]
        self.assertNotIn(self.retired.id, ids)

    def test_non_directors_are_not_swept_in_by_this(self):
        # The company-wide reach is for Directors specifically — a Manager who
        # approves nothing here should still hear nothing.
        quiet = User.objects.create(
            email='quiet@dbn.com', company=self.co, role='Manager', designation='Manager',
            user_code='D8', name='Unrelated Mgr')
        ids = [s[0] for s in self._event('sales_approved', actor=self.admin)]
        self.assertNotIn(quiet.id, ids)
