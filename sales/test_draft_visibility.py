"""Who can see a draft booking in Approvals.

Drafts are half-finished commercial terms, so they were private to their author —
which left an approver looking at a plot map full of drafted units and a Drafts tab
saying "no bookings here". They are now visible to the author, to a real admin, and
to whoever approves that project's bookings: the same people who can already cancel
a drafted unit from the map.

Which approver list governs follows the booking's own routing, as approve and reject
do — a CP-sourced draft answers to the CP list, not the regular one.
"""
from django.core.cache import cache
from rest_framework.test import APITestCase

from companies.models import Company
from accounts.models import User
from sales.models import Booking, Project

from sales.tests import auth


class DraftVisibilityTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DRV', name='Draft Co')
        cls.admin = User.objects.create(email='drv_admin@x.com', company=cls.co,
                                        role='Admin', is_staff=True, user_code='V0')
        cls.author = User.objects.create(email='drv_stm@x.com', company=cls.co,
                                         role='STM', designation='STM', user_code='V1')
        cls.approver = User.objects.create(email='drv_mgr@x.com', company=cls.co,
                                           role='Manager', user_code='V2')
        cls.cp_approver = User.objects.create(email='drv_cp@x.com', company=cls.co,
                                              role='Manager', user_code='V3')
        cls.bystander = User.objects.create(email='drv_other@x.com', company=cls.co,
                                            role='Manager', user_code='V4')
        cls.project = Project.objects.create(
            company=cls.co, name='Tower', booking_approvers=[cls.approver.id],
            cp_booking_approvers=[cls.cp_approver.id])
        cls.draft = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.author, status='draft',
            client_name='Sales Draft', phone='9000000001')
        cls.cp_draft = Booking.objects.create(
            company=cls.co, project=cls.project, stm=cls.author, status='draft',
            source='Channel Partner', client_name='CP Draft', phone='9000000002')

    def setUp(self):
        cache.clear()

    def _drafts_for(self, user):
        auth(self.client, user)
        r = self.client.get('/api/sales/bookings/?status=draft')
        self.assertEqual(r.status_code, 200)
        return sorted(b['client_name'] for b in r.data)

    def test_the_author_sees_their_own(self):
        self.assertEqual(self._drafts_for(self.author), ['CP Draft', 'Sales Draft'])

    def test_an_admin_sees_every_draft(self):
        self.assertEqual(self._drafts_for(self.admin), ['CP Draft', 'Sales Draft'])

    def test_the_projects_approver_sees_its_sales_draft(self):
        self.assertIn('Sales Draft', self._drafts_for(self.approver))

    def test_a_sales_approver_does_not_see_the_cp_draft(self):
        """The two approver lists exist to separate CP from regular — this must not
        quietly reunite them."""
        self.assertNotIn('CP Draft', self._drafts_for(self.approver))

    def test_the_cp_approver_sees_the_cp_draft_only(self):
        self.assertEqual(self._drafts_for(self.cp_approver), ['CP Draft'])

    def test_a_manager_who_approves_nothing_here_sees_none(self):
        """Being a manager is not itself authority, same as approve/reject/cancel."""
        self.assertEqual(self._drafts_for(self.bystander), [])

    def test_drafts_still_stay_out_of_the_all_tab_for_outsiders(self):
        auth(self.client, self.bystander)
        r = self.client.get('/api/sales/bookings/')
        self.assertNotIn('Sales Draft', [b['client_name'] for b in r.data])
        self.assertNotIn('CP Draft', [b['client_name'] for b in r.data])
