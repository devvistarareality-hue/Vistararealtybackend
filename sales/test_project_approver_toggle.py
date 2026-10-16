"""Ticking two approvers quickly must not throw one of them away.

The picker used to read a project's approver list in the browser, compute the
new array and PATCH the whole thing. Two toggles for the same project in flight
together each computed their array from the same stale copy, so whichever write
landed last at the DB won and the other click vanished — no error on either
side. The activity log caught it doing exactly that here: "4 → 4, 5" and then,
in the same second, "4, 5 → —", the second request having computed [] from a
stale [4]. Every Sales and CP approver list on the company was emptied that
way.

The endpoint takes the one id being toggled and serializes on the project's row
lock, so the result cannot depend on how the requests interleave. The
concurrency test below is the point of the file; the rest guards the gates
around it, since this endpoint writes a permission-bearing list.
"""
import threading

import unittest

from django.db import connection, connections
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Project


class ProjectApproverToggleTests(TransactionTestCase):
    """TransactionTestCase, not TestCase: select_for_update only actually
    serializes against committed rows, which a test wrapped in one open
    transaction would never see from another thread."""

    reset_sequences = True

    def setUp(self):
        self.co = Company.objects.create(code='PAT', name='Approver Co', is_active=True)
        self.admin = User.objects.create_user(
            'admin@pat.com', company=self.co, user_code='PAT001', password='p',
            name='Real Admin', role='Admin', modules=['Sales'])
        self.sales_admin = User.objects.create_user(
            'sadmin@pat.com', company=self.co, user_code='PAT002', password='p',
            name='Sales Module Admin', role='Manager', modules=['Sales'],
            admin_modules=['Sales'])
        self.manager = User.objects.create_user(
            'mgr@pat.com', company=self.co, user_code='PAT003', password='p',
            name='Plain Manager', role='Manager', modules=['Sales'])
        self.a = User.objects.create_user(
            'a@pat.com', company=self.co, user_code='PAT004', password='p',
            name='Approver A', role='Manager', modules=['Sales'])
        self.b = User.objects.create_user(
            'b@pat.com', company=self.co, user_code='PAT005', password='p',
            name='Approver B', role='Manager', modules=['Sales'])
        self.project = Project.objects.create(company=self.co, name='Kalrav')

    def _api(self, user):
        api = APIClient()
        api.force_authenticate(user=user)
        return api

    def _toggle(self, user, user_id, field='booking_approvers'):
        return self._api(user).post(
            f'/api/sales/projects/{self.project.pk}/toggle-approver/',
            {'user_id': user_id, 'field': field}, format='json')

    def _stored(self, field='booking_approvers'):
        return getattr(Project.objects.get(pk=self.project.pk), field)

    # ------------------------------------------------------------ the race

    def test_two_approvers_ticked_at_once_both_survive(self):
        """The whole reason this endpoint exists. Both toggles are sent
        together, as two quick clicks are; neither may be lost."""
        errors = []

        def tick(uid):
            try:
                self._toggle(self.admin, uid)
            except Exception as exc:          # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=tick, args=(u.id,)) for u in (self.a, self.b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(sorted(self._stored()), sorted([self.a.id, self.b.id]),
                         'a concurrent toggle was dropped — the bug this endpoint fixes')

    # Six writers at once is a Postgres row-lock test. SQLite (the local and CI test
    # database) has no row locks — it refuses concurrent writers with "database is
    # locked", which the threads swallow, so the list stays empty there. The two-
    # writer test above still runs everywhere.
    @unittest.skipUnless(connection.vendor == 'postgresql', 'needs real row locks (Postgres)')
    def test_many_toggles_at_once_all_survive(self):
        people = [User.objects.create_user(
            f'p{i}@pat.com', company=self.co, user_code=f'PAT1{i:02d}', password='p',
            name=f'Person {i}', role='Manager', modules=['Sales']) for i in range(6)]

        def tick(uid):
            try:
                self._toggle(self.admin, uid)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=tick, args=(p.id,)) for p in people]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(self._stored()), sorted(p.id for p in people))

    # ------------------------------------------------------------- toggling

    def test_a_toggle_adds_then_removes(self):
        self._toggle(self.admin, self.a.id)
        self.assertEqual(self._stored(), [self.a.id])
        self._toggle(self.admin, self.a.id)
        self.assertEqual(self._stored(), [])

    def test_the_response_carries_the_saved_list(self):
        # The client replaces its optimistic copy with this, so it has to be
        # the real post-write state.
        r = self._toggle(self.admin, self.a.id)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['booking_approvers'], [self.a.id])

    def test_each_list_is_independent(self):
        self._toggle(self.admin, self.a.id, 'booking_approvers')
        self._toggle(self.admin, self.b.id, 'cp_booking_approvers')
        self.assertEqual(self._stored('booking_approvers'), [self.a.id])
        self.assertEqual(self._stored('cp_booking_approvers'), [self.b.id])

    # --------------------------------------------------------------- the gates

    def test_a_sales_admin_may_set_booking_approvers(self):
        self.assertEqual(self._toggle(self.sales_admin, self.a.id).status_code, 200)

    def test_a_plain_manager_may_not(self):
        """Otherwise an approver restricted to one project could add themselves
        to another and approve it."""
        self.assertEqual(self._toggle(self.manager, self.a.id).status_code, 403)
        self.assertEqual(self._stored(), [])

    def test_only_a_real_admin_may_set_the_accounts_list(self):
        # Tighter than the Sales list on purpose: admin rights over a module do
        # not extend to deciding who signs off at the money stage.
        self.assertEqual(
            self._toggle(self.sales_admin, self.a.id, 'accounts_booking_approvers').status_code, 403)
        self.assertEqual(
            self._toggle(self.admin, self.a.id, 'accounts_booking_approvers').status_code, 200)

    def test_an_unknown_list_is_refused(self):
        # Without this the field name would be a free write into any column.
        r = self._toggle(self.admin, self.a.id, 'is_active')
        self.assertEqual(r.status_code, 400)

    def test_a_user_from_another_company_is_refused(self):
        """A project that looks configured but names someone who cannot act on
        it can never be approved — worse than an empty list, which at least
        falls back to an administrator."""
        other = Company.objects.create(code='PAU', name='Elsewhere', is_active=True)
        outsider = User.objects.create_user(
            'out@pau.com', company=other, user_code='PAU001', password='p',
            name='Outsider', role='Manager', modules=['Sales'])
        self.assertEqual(self._toggle(self.admin, outsider.id).status_code, 400)
        self.assertEqual(self._stored(), [])

    def test_a_missing_user_id_is_refused(self):
        r = self._api(self.admin).post(
            f'/api/sales/projects/{self.project.pk}/toggle-approver/',
            {'field': 'booking_approvers'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_another_company_cannot_touch_this_project(self):
        other = Company.objects.create(code='PAV', name='Third Party', is_active=True)
        their_admin = User.objects.create_user(
            'a@pav.com', company=other, user_code='PAV001', password='p',
            name='Their Admin', role='Admin', modules=['Sales'])
        self.assertEqual(self._toggle(their_admin, self.a.id).status_code, 404)
        self.assertEqual(self._stored(), [])
