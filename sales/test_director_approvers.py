"""A Director can be appointed an approver in any module.

Every other approver pool is built from access to that module — you can be
named on Accounts approvals because you have Accounts, on Channel Partner
approvals because you work the partner desk. A Director's authority does not
come from a module grant, so those tests quietly kept them off the pickers for
modules nobody had granted them: on the live company none of the four Directors
could be named a CP approver, and only the two with an Accounts grant could be
named there.

Being listed is not authority. These tests also pin the other half: a Director
who has not been named on a project still cannot approve its bookings. The
picker decides who may be appointed; the project's own list decides who did get
appointed, and that is still the thing checked at approval time.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from companies.models import Company
from sales.models import Project
from sales.views import _can_approve_project


class DirectorApproverPoolTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.co = Company.objects.create(code='DIR', name='Director Co', is_active=True)
        cls.other_co = Company.objects.create(code='DIS', name='Elsewhere', is_active=True)
        cls.admin = User.objects.create_user(
            'admin@dir.com', company=cls.co, user_code='DIR001', password='p',
            name='The Admin', role='Admin', modules=['Sales'])
        # A Director with no module grants at all — the case that was missing.
        cls.bare = User.objects.create_user(
            'bare@dir.com', company=cls.co, user_code='DIR002', password='p',
            name='Bare Director', role='Director', modules=[])
        # A Director granted only Sales: still not CP, still not Accounts.
        cls.sales_only = User.objects.create_user(
            'sales@dir.com', company=cls.co, user_code='DIR003', password='p',
            name='Sales Director', role='Director', modules=['Sales'])
        # Someone senior who is NOT a Director — the control.
        cls.gm = User.objects.create_user(
            'gm@dir.com', company=cls.co, user_code='DIR004', password='p',
            name='A General Manager', role='General Manager', modules=['Sales'])
        cls.stm = User.objects.create_user(
            'stm@dir.com', company=cls.co, user_code='DIR005', password='p',
            name='Plain STM', role='Employee', designation='STM', modules=['Sales'])
        # A Director at another company must never leak in.
        cls.theirs = User.objects.create_user(
            'd@dis.com', company=cls.other_co, user_code='DIS001', password='p',
            name='Their Director', role='Director', modules=[])
        cls.project = Project.objects.create(company=cls.co, name='Skyline')

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(user=self.admin)

    def _names(self, crm_role):
        r = self.api.get(f'/api/sales/users/telecallers/?crm_role={crm_role}')
        self.assertEqual(r.status_code, 200, r.data)
        return {u['name'] for u in r.data}

    # ----------------------------------------------- the two pools that missed them

    def test_a_director_can_be_named_a_cp_approver_without_cp_access(self):
        names = self._names('cp_module')
        self.assertIn('Bare Director', names)
        self.assertIn('Sales Director', names)

    def test_a_director_can_be_named_an_accounts_approver_without_accounts_access(self):
        names = self._names('accounts_module')
        self.assertIn('Bare Director', names)
        self.assertIn('Sales Director', names)

    # ------------------------------------------------- the pools that already had them

    def test_a_director_is_offered_for_sales_booking_approvals(self):
        # Sourced from DistributionSettings' manager list rather than this
        # endpoint, so it is checked where it actually comes from.
        r = self.api.get('/api/sales/dist-settings/')
        self.assertEqual(r.status_code, 200, r.data)
        names = {m['name'] for m in r.data['managers']}
        self.assertIn('Bare Director', names)
        self.assertIn('Sales Director', names)

    def test_a_director_is_offered_for_project_approvals(self):
        r = self.api.get('/api/sales/projects/approvers/')
        self.assertEqual(r.status_code, 200, r.data)
        names = {p['name'] for p in r.data['people']}
        self.assertIn('Bare Director', names)

    # ------------------------------------------------------- what must not change

    def test_being_a_director_is_not_itself_approval_authority(self):
        """The whole safety property. Appearing in the picker only means
        appointable — a Director nobody named on this project still cannot
        approve its bookings."""
        self.project.cp_booking_approvers = []
        self.project.save(update_fields=['cp_booking_approvers'])
        self.assertFalse(_can_approve_project(self.bare, self.project, self.co))

    def test_a_director_who_was_named_can_approve(self):
        self.project.booking_approvers = [self.bare.id]
        self.project.save(update_fields=['booking_approvers'])
        self.assertTrue(_can_approve_project(self.bare, self.project, self.co))

    def test_a_director_at_another_company_is_never_offered(self):
        for pool in ('cp_module', 'accounts_module'):
            self.assertNotIn('Their Director', self._names(pool), pool)

    def test_seniority_alone_does_not_get_you_in(self):
        """A General Manager is senior but not a Director. The CP pool is still
        about the partner desk for everyone else — otherwise this change would
        quietly turn an access-based picker into a seniority-based one."""
        self.assertNotIn('A General Manager', self._names('cp_module'))
        self.assertNotIn('A General Manager', self._names('accounts_module'))

    def test_a_plain_stm_is_still_not_offered_for_accounts(self):
        self.assertNotIn('Plain STM', self._names('accounts_module'))

    def test_the_strict_partner_desk_pool_is_untouched(self):
        """cp_only exists precisely to exclude everyone who is not CP-designated.
        A Director is not partner-desk staff, so they do not belong here."""
        self.assertNotIn('Bare Director', self._names('cp_only'))
