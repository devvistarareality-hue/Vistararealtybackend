"""A rep (Employee / Intern) assigned to projects books only on those projects.

Select Project (?for_booking=1) lists only the assigned ones, and the write paths —
holding a unit, saving a draft, submitting a booking — refuse the others. Managers,
admins and reps with nothing assigned are not restricted.
"""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from companies.models import Company
from sales.models import Plot, Project, UserProjectAssignment
from sales.views import (BookingDraftView, BookingListCreateView, PlotHoldView,
                         ProjectListView, booking_project_ids)


class BookingProjectScope(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='BP', name='BP Co')
        self.alpha = Project.objects.create(company=self.co, name='Alpha', is_active=True, approval_status='approved')
        self.beta = Project.objects.create(company=self.co, name='Beta', is_active=True, approval_status='approved')
        self.beta_plot = Plot.objects.create(project=self.beta, number='B1', status='available')

        self.stm = self._user('stm@x.com', 'S1', 'Employee')
        UserProjectAssignment.objects.create(user=self.stm, project=self.alpha)
        self.free_stm = self._user('free@x.com', 'S2', 'Employee')     # nothing assigned
        self.manager = self._user('mgr@x.com', 'M1', 'Manager')
        UserProjectAssignment.objects.create(user=self.manager, project=self.alpha)
        self.admin = self._user('adm@x.com', 'A1', 'Admin')

    def _user(self, email, code, role):
        return User.objects.create(name=code, email=email, phone='9' + code + '00000',
                                   user_code=code, role=role, company=self.co)

    def _call(self, view, user, method='get', path='/x/', data=None):
        f = APIRequestFactory()
        req = f.get(path) if method == 'get' else f.post(path, data or {}, format='json')
        force_authenticate(req, user=user)
        return view.as_view()(req)

    def _names(self, user, qs='?for_booking=1'):
        return sorted(p['name'] for p in self._call(ProjectListView, user, path='/x/' + qs).data)

    # ── Select Project ───────────────────────────────────────────────────────
    def test_assigned_rep_picks_only_their_projects_for_booking(self):
        self.assertEqual(self._names(self.stm), ['Alpha'])

    def test_other_project_pickers_are_unchanged(self):
        """Lead / site-visit filters don't ask for_booking, so they still list all."""
        self.assertEqual(self._names(self.stm, ''), ['Alpha', 'Beta'])

    def test_rep_with_nothing_assigned_is_not_restricted(self):
        self.assertIsNone(booking_project_ids(self.free_stm))
        self.assertEqual(self._names(self.free_stm), ['Alpha', 'Beta'])

    def test_manager_and_admin_book_on_any_project(self):
        self.assertIsNone(booking_project_ids(self.manager))
        self.assertEqual(self._names(self.manager), ['Alpha'])   # manager scope, unchanged
        UserProjectAssignment.objects.create(user=self.admin, project=self.alpha)
        self.assertEqual(self._names(self.admin), ['Alpha', 'Beta'])

    # ── write paths ─────────────────────────────────────────────────────────
    def test_rep_cannot_hold_a_unit_on_another_project(self):
        res = self._call(PlotHoldView, self.stm, 'post', data={'plot_ids': [self.beta_plot.id]})
        self.assertEqual([f['reason'] for f in res.data['failed']], ['not_assigned'])
        self.beta_plot.refresh_from_db()
        self.assertEqual(self.beta_plot.status, 'available')

    def test_rep_cannot_book_or_draft_on_another_project(self):
        for view in (BookingListCreateView, BookingDraftView):
            res = self._call(view, self.stm, 'post', data={'project': self.beta.id})
            self.assertEqual(res.status_code, 403, view.__name__)
            self.assertIn('not assigned', res.data['detail'])
