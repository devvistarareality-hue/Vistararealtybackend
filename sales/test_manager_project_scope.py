"""A manager assigned to projects sees only those projects' leads/SVs/closures.

Bookings are deliberately exempt: a manager may book a plot on any project.
"""
from datetime import date

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from companies.models import Company
from sales.models import (Closure, FollowUp, Lead, Plot, Project, SiteVisit,
                          UserProjectAssignment)
from sales.views import (BookingListCreateView, ClosureListView, FollowUpListView,
                         LeadListView, SiteVisitListView, manager_project_ids)


class ManagerProjectScope(TestCase):
    def setUp(self):
        cache.clear()
        self.co = Company.objects.create(code='MP', name='MP Co')
        self.alpha = Project.objects.create(company=self.co, name='Alpha')
        self.beta = Project.objects.create(company=self.co, name='Beta')

        self.scoped = self._user('scoped@x.com', 'M1', 'Manager')
        UserProjectAssignment.objects.create(user=self.scoped, project=self.alpha)
        self.unscoped = self._user('open@x.com', 'M2', 'Manager')       # no projects
        self.admin = self._user('adm@x.com', 'A1', 'Admin')
        self.stm = self._user('stm@x.com', 'S1', 'Employee')

        self.l_alpha = self._lead(self.alpha, 'Alpha Lead')
        self.l_beta = self._lead(self.beta, 'Beta Lead')

    def _user(self, email, code, role):
        return User.objects.create(name=code, email=email, phone='9' + code + '00000',
                                   user_code=code, role=role, company=self.co)

    def _lead(self, project, name):
        lead = Lead.objects.create(company=self.co, name=name, phone='9' + str(abs(hash(name)) % 10**9),
                                   project=project, stm=self.stm)
        SiteVisit.objects.create(lead=lead, project=project, stm=self.stm,
                                 scheduled_at='2026-08-01T10:00:00Z')
        Closure.objects.create(company=self.co, lead=lead, project=project, stm=self.stm,
                               closure_date=date(2026, 8, 1))
        FollowUp.objects.create(lead=lead, assigned_to=self.stm,
                                scheduled_at='2026-08-02T10:00:00Z')
        return lead

    def _get(self, view, user, path='/x/'):
        req = APIRequestFactory().get(path)
        force_authenticate(req, user=user)
        res = view.as_view()(req)
        d = res.data
        return d.get('results', d) if isinstance(d, dict) else d

    # ── the restriction ──────────────────────────────────────────────────────
    def test_scoped_manager_sees_only_their_projects_leads(self):
        names = [r['name'] for r in self._get(LeadListView, self.scoped, '/x/?page=1')]
        self.assertEqual(names, ['Alpha Lead'])

    def test_scoped_manager_sees_only_their_projects_site_visits(self):
        rows = self._get(SiteVisitListView, self.scoped)
        self.assertEqual([r['project_name'] for r in rows], ['Alpha'])

    def test_scoped_manager_sees_only_their_projects_closures(self):
        rows = self._get(ClosureListView, self.scoped)
        self.assertEqual([r['project_name'] for r in rows], ['Alpha'])

    def test_scoped_manager_sees_only_their_projects_followups(self):
        rows = self._get(FollowUpListView, self.scoped)
        self.assertEqual(len(rows), 1)

    # ── who is NOT restricted ────────────────────────────────────────────────
    def test_manager_with_no_projects_still_sees_everything(self):
        """Assignment is the opt-in — nobody loses access by default."""
        names = sorted(r['name'] for r in self._get(LeadListView, self.unscoped, '/x/?page=1'))
        self.assertEqual(names, ['Alpha Lead', 'Beta Lead'])

    def test_admin_is_never_project_scoped(self):
        UserProjectAssignment.objects.create(user=self.admin, project=self.alpha)
        self.assertIsNone(manager_project_ids(self.admin))
        names = sorted(r['name'] for r in self._get(LeadListView, self.admin, '/x/?page=1'))
        self.assertEqual(names, ['Alpha Lead', 'Beta Lead'])

    def test_director_and_gm_are_scoped_the_same_way(self):
        for role in ('Director', 'General Manager'):
            u = self._user(f'{role}@x.com', role[:2].upper() + '9', role)
            UserProjectAssignment.objects.create(user=u, project=self.beta)
            self.assertEqual(manager_project_ids(u), [self.beta.id], role)

    # ── bookings stay open ───────────────────────────────────────────────────
    def test_project_scoping_does_not_change_booking_visibility(self):
        """Bookings scope by reporting tree / approver, never by project assignment.

        Asserted as a comparison rather than an absolute count: a manager already
        sees only their own tree's bookings, which is existing behaviour this change
        must leave exactly as it found it.
        """
        from sales.models import Booking
        plot = Plot.objects.create(project=self.beta, number='B-1')
        Booking.objects.create(company=self.co, project=self.beta, plot=plot,
                               client_name='C', status='sold', approval_status='APPROVED')
        scoped = self._get(BookingListCreateView, self.scoped)
        unscoped = self._get(BookingListCreateView, self.unscoped)
        self.assertEqual(len(scoped), len(unscoped))

    def test_a_scoped_manager_can_still_book_a_plot_on_another_project(self):
        """The explicit requirement: restricted on leads, unrestricted on booking."""
        plot = Plot.objects.create(project=self.beta, number='B-2')
        req = APIRequestFactory().post('/x/', {
            'project': self.beta.id, 'plot': plot.id,
            'client_name': 'Buyer', 'phone': '9000000000',
        }, format='json')
        force_authenticate(req, user=self.scoped)
        res = BookingListCreateView.as_view()(req)
        self.assertIn(res.status_code, (200, 201),
                      f'booking another project must be allowed, got {res.status_code} {res.data}')
