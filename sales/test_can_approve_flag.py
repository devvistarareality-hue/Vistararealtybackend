"""Whether the Approve/Reject buttons are offered on a booking.

Routing is per booking, not per person: a Channel-Partner-sourced deal answers to
the project's CP approvers and everything else to its regular ones. The CP module
was offering Approve and Reject on every pending row it listed, including walk-ins
the CP manager had booked himself — the server refused the action, so the click
just failed silently, which reads as a broken button rather than as the booking not
being his to decide.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Project

from sales.tests import auth


class CanApproveFlagTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='CAF', name='CanApprove Co')
        cls.sales_approver = User.objects.create(
            email='caf_sales@x.com', company=cls.co, role='Manager', user_code='F1')
        cls.cp_approver = User.objects.create(
            email='caf_cp@x.com', company=cls.co, role='Manager',
            designation='CP CLUSTER HEAD', user_code='F2')
        cls.project = Project.objects.create(
            company=cls.co, name='Kalrav',
            booking_approvers=[cls.sales_approver.id],
            cp_booking_approvers=[cls.cp_approver.id])
        cls.walkin = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.cp_approver, status='pending',
            source='walk-in', client_name='Walk-in Deal', phone='9000000050')
        cls.cp_deal = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.cp_approver, status='pending',
            source='Channel Partner', client_name='CP Deal', phone='9000000051')

    def setUp(self):
        cache.clear()

    def _flags(self, user):
        auth(self.client, user)
        r = self.client.get('/api/sales/bookings/?status=pending')
        self.assertEqual(r.status_code, 200)
        return {b['client_name']: b['can_approve'] for b in r.data}

    def test_a_cp_approver_may_not_action_a_walk_in_they_booked(self):
        """The reported case: it routes to the Sales approvers, not to him."""
        self.assertIs(self._flags(self.cp_approver).get('Walk-in Deal'), False)

    def test_a_cp_approver_may_action_the_cp_deal(self):
        self.assertIs(self._flags(self.cp_approver).get('CP Deal'), True)

    def test_the_sales_approver_may_action_the_walk_in(self):
        self.assertIs(self._flags(self.sales_approver).get('Walk-in Deal'), True)

    def test_the_sales_approver_never_even_sees_the_cp_deal(self):
        """Routing keeps the two pools apart upstream of the flag: a CP-sourced deal
        is not in the Sales approver's list at all, so there is nothing to gate."""
        self.assertNotIn('CP Deal', self._flags(self.sales_approver))

    def test_the_flag_matches_what_the_action_endpoint_does(self):
        """The button must not promise something the server will refuse."""
        auth(self.client, self.cp_approver)
        r = self.client.post('/api/sales/bookings/%d/action/' % self.walkin.id,
                             {'action': 'approve'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.walkin.refresh_from_db()
        self.assertEqual(self.walkin.status, 'pending')
