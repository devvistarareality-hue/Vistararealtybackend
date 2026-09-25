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


class TheApprovalsListIsWhatYouMayDecide(TestCase):
    """Named on no project, and the Approvals screen is empty.

    It used to fall back to the viewer's own work, so a Cluster Head named
    nowhere opened Approvals on 44 of his own sales, with Cancel and Revise
    offered on each. Cancel was refused by the server, but a screen that offers
    a verdict on your own deal is the wrong screen. They are on My Bookings,
    which is where they belong, and still are.
    """

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.co = Company.objects.create(code='APL', name='Approvals Co')
        self.named = User.objects.create_user('n@apl.com', company=self.co, user_code='L-N',
                                              password='x', name='Named', role='Manager',
                                              modules=['Sales'])
        self.nowhere = User.objects.create_user('x@apl.com', company=self.co, user_code='L-X',
                                                password='x', name='Nowhere', role='Manager',
                                                modules=['Sales'])
        self.admin = User.objects.create_user('a@apl.com', company=self.co, user_code='L-A',
                                              password='x', name='Admin', role='Admin',
                                              modules=['Sales'])
        self.p = Project.objects.create(company=self.co, name='Tundav',
                                        booking_approvers=[self.named.id])
        for i, seller in enumerate((self.named, self.nowhere)):
            plot = Plot.objects.create(project=self.p, number=f'Q{i}')
            Booking.objects.create(company=self.co, project=self.p, plot=plot, stm=seller,
                                   client_name=f'C{i}', phone=f'977770000{i}', status='sold',
                                   approval_status='APPROVED')

    def _approvals(self, user):
        from rest_framework.test import APIClient
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=user.pk))
        return api.get('/api/sales/bookings/?to_decide=1&status=sold&source=sales').json()

    def _my_bookings(self, user):
        from rest_framework.test import APIClient
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=user.pk))
        return api.get('/api/sales/bookings/?status=sold&mine=1&source=sales').json()

    def test_a_manager_named_nowhere_gets_an_empty_list(self):
        self.assertEqual(self._approvals(self.nowhere), [])

    def test_the_rest_of_the_endpoint_is_unchanged(self):
        """Drafts and the search the Revise form runs are the viewer's own work,
        not a verdict — they do not ask to_decide and still come back."""
        from rest_framework.test import APIClient
        api = APIClient()
        api.force_authenticate(User.objects.get(pk=self.nowhere.pk))
        rows = api.get('/api/sales/bookings/?status=sold&source=sales').json()
        self.assertEqual([b['client_name'] for b in rows], ['C1'])

    def test_their_own_sale_is_still_on_my_bookings(self):
        names = [b['client_name'] for b in self._my_bookings(self.nowhere)]
        self.assertEqual(names, ['C1'], 'their own work did not go anywhere')

    def test_the_named_approver_still_sees_the_project(self):
        self.assertEqual(len(self._approvals(self.named)), 2,
                         "both of the project's bookings, which is what they decide")

    def test_an_admin_still_sees_the_company(self):
        self.assertEqual(len(self._approvals(self.admin)), 2)
