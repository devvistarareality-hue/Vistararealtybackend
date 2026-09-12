"""Cancelling a unit's soft hold.

A unit sits at status='hold' both when someone has it selected on the map and when
a booking against it is waiting on a manager; submission is what clears held_by.
Only the first is cancellable here — the second is an approval to reject.

Who may cancel: the person holding it, a real admin, and whoever approves that
project's bookings. CP-sourced units follow the separate CP approver list.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Plot, Project

from sales.tests import auth


class CancelHoldTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CXL', name='Cancel Co')
        cls.admin = User.objects.create(email='cxl_admin@x.com', company=cls.co,
                                        role='Admin', is_staff=True, user_code='XA')
        cls.stm = User.objects.create(email='cxl_stm@x.com', company=cls.co,
                                      role='STM', designation='STM', user_code='XS')
        cls.other_stm = User.objects.create(email='cxl_stm2@x.com', company=cls.co,
                                            role='STM', designation='STM', user_code='XT')
        cls.approver = User.objects.create(email='cxl_mgr@x.com', company=cls.co,
                                           role='Manager', user_code='XM')
        cls.bystander = User.objects.create(email='cxl_mgr2@x.com', company=cls.co,
                                            role='Manager', user_code='XN')
        cls.cp_approver = User.objects.create(email='cxl_cp@x.com', company=cls.co,
                                              role='Manager', user_code='XC')
        cls.project = Project.objects.create(
            company=cls.co, name='Tower X',
            booking_approvers=[cls.approver.id], cp_booking_approvers=[cls.cp_approver.id])

    def setUp(self):
        cache.clear()

    def _held(self, number='X-1', by=None):
        return Plot.objects.create(project=self.project, number=number,
                                   status=Plot.HOLD, held_by=by or self.stm,
                                   pre_hold_status='available')

    def _cancel(self, user, plot):
        auth(self.client, user)
        return self.client.post('/api/sales/plots/cancel-hold/',
                                {'plot_ids': [plot.id]}, format='json')

    # ── who may cancel ───────────────────────────────────────────────────
    def test_the_holder_can_cancel_their_own(self):
        p = self._held()
        self.assertEqual(self._cancel(self.stm, p).status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.status, Plot.AVAILABLE)
        self.assertIsNone(p.held_by_id)

    def test_the_projects_approver_can_cancel_another_stms(self):
        p = self._held()
        self.assertEqual(self._cancel(self.approver, p).status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.status, Plot.AVAILABLE)

    def test_an_admin_can_cancel_anyones(self):
        p = self._held()
        self.assertEqual(self._cancel(self.admin, p).status_code, 200)

    def test_a_manager_who_does_not_approve_this_project_cannot(self):
        """Being a manager is not itself authority — the same rule approve/reject uses."""
        p = self._held()
        self.assertEqual(self._cancel(self.bystander, p).status_code, 403)
        p.refresh_from_db()
        self.assertEqual(p.status, Plot.HOLD)

    def test_another_stm_cannot(self):
        p = self._held()
        self.assertEqual(self._cancel(self.other_stm, p).status_code, 403)
        p.refresh_from_db()
        self.assertEqual(p.status, Plot.HOLD)

    # ── what must not be cancellable ─────────────────────────────────────
    def test_a_submitted_booking_is_refused(self):
        """held_by is cleared at submission; that unit is an approval to reject."""
        p = Plot.objects.create(project=self.project, number='X-9',
                                status=Plot.HOLD, held_by=None)
        r = self._cancel(self.approver, p)
        self.assertEqual(r.status_code, 400)
        self.assertIn('Approvals', r.data['detail'])
        p.refresh_from_db()
        self.assertEqual(p.status, Plot.HOLD)

    # ── side effects ─────────────────────────────────────────────────────
    def test_a_resale_unit_returns_to_resale_not_available(self):
        p = Plot.objects.create(project=self.project, number='X-5', status=Plot.HOLD,
                                held_by=self.stm, pre_hold_status='resale')
        self._cancel(self.approver, p)
        p.refresh_from_db()
        self.assertEqual(p.status, 'resale')

    def test_cancelling_a_drafted_unit_discards_the_draft(self):
        """The draft pins the hold; leaving it would point at a unit others can book."""
        p = self._held('X-7')
        d = Booking.objects.create(company=self.co, project=self.project, plot=p,
                                   stm=self.stm, status='draft', client_name='D', phone='9')
        r = self._cancel(self.approver, p)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['drafts_discarded'], 1)
        self.assertFalse(Booking.objects.filter(pk=d.pk).exists())

    def test_cp_units_follow_the_cp_approver_list(self):
        """Routing comes off the booking's own Source, exactly as approve/reject does."""
        p = self._held('X-3')
        Booking.objects.create(company=self.co, project=self.project, plot=p,
                               stm=self.stm, status='draft', source='Channel Partner',
                               client_name='C', phone='9')
        # the Sales approver does not govern a CP unit
        self.assertEqual(self._cancel(self.approver, p).status_code, 403)
        self.assertEqual(self._cancel(self.cp_approver, p).status_code, 200)
