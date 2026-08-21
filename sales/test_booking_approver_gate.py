"""Only the managers named on a project may approve its bookings."""
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from companies.models import Company
from sales.models import Booking, Plot, Project
from sales.views import BookingActionView, _can_approve_project


class ApproverGate(TestCase):
    def setUp(self):
        self.co = Company.objects.create(code='AP', name='Approver Co')
        self.named = self._user('named@x.com', 'N1', 'Manager')
        self.other = self._user('other@x.com', 'O1', 'Manager')   # a manager, named nowhere
        self.admin = self._user('adm@x.com', 'A1', 'Admin')
        self.stm = self._user('stm@x.com', 'S1', 'Employee')

        self.configured = Project.objects.create(company=self.co, name='Configured',
                                                 booking_approvers=[self.named.id])
        self.unconfigured = Project.objects.create(company=self.co, name='Unconfigured',
                                                   booking_approvers=[])

    def _user(self, email, code, role):
        return User.objects.create(name=code, email=email, phone='9' + code + '0000',
                                   user_code=code, role=role, company=self.co)

    def _booking(self, project):
        plot = Plot.objects.create(project=project, number='P1')
        return Booking.objects.create(company=self.co, project=project, plot=plot,
                                      stm=self.stm, client_name='C', status='pending',
                                      approval_status='PENDING')

    def _approve(self, actor, booking):
        req = APIRequestFactory().post('/x/', {'action': 'approve'}, format='json')
        force_authenticate(req, user=actor)
        return BookingActionView.as_view()(req, pk=booking.id)

    # ── a configured project ────────────────────────────────────────────────
    def test_named_approver_can_approve(self):
        b = self._booking(self.configured)
        self.assertEqual(self._approve(self.named, b).status_code, 200)

    def test_manager_not_named_cannot_approve(self):
        b = self._booking(self.configured)
        self.assertEqual(self._approve(self.other, b).status_code, 403)
        b.refresh_from_db()
        self.assertEqual(b.approval_status, 'PENDING')

    # ── a project nobody is configured for ──────────────────────────────────
    def test_unconfigured_project_is_not_open_to_every_manager(self):
        """The reported bug: an empty approver list used to mean 'anyone'."""
        b = self._booking(self.unconfigured)
        self.assertEqual(self._approve(self.other, b).status_code, 403)
        self.assertEqual(self._approve(self.named, b).status_code, 403)
        b.refresh_from_db()
        self.assertEqual(b.approval_status, 'PENDING')

    def test_admin_can_still_approve_an_unconfigured_project(self):
        """Someone has to be able to, or a booking could never be cleared."""
        b = self._booking(self.unconfigured)
        self.assertEqual(self._approve(self.admin, b).status_code, 200)

    # ── the helper itself ───────────────────────────────────────────────────
    def test_gate_matrix(self):
        for user, project, expected in [
            (self.named, self.configured, True),
            (self.named, self.unconfigured, False),
            (self.other, self.configured, False),
            (self.other, self.unconfigured, False),
            (self.admin, self.configured, True),
            (self.admin, self.unconfigured, True),
        ]:
            got = _can_approve_project(user, project, self.co)
            self.assertEqual(got, expected, f'{user.name} on {project.name}')

    def test_cancel_uses_the_same_gate(self):
        from sales.views import ClosureCancelView
        self.assertTrue(hasattr(ClosureCancelView, 'post'))
        self.assertFalse(_can_approve_project(self.other, self.configured, self.co))
